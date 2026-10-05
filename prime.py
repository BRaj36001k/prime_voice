#!/usr/bin/env python3
"""
Prime — desktop clap listener.

Listens to the microphone and, on a double clap, runs the Prime startup:
  - speaks the welcome line (ElevenLabs TTS, cached as WAV after the first fetch)
  - optionally brings Cursor to the front (Windows)

No websites, songs or browser windows are opened at startup.

Run:
  python -m pip install -r requirements.txt
  python prime.py

Tuning (constants below):
  SAMPLE_RATE, BLOCK_MS, SPIKE_RATIO, COOLDOWN_S, MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S,
  RETRIGGER_RATIO, NOISE_FLOOR_ALPHA, MIN_RMS — clap detection (see README).
  PRIME_WELCOME_* — startup speech. Configure keys in `.env` or `api.env` next to this script
    (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ...). Audio is cached under `.cache/prime_welcome/`.
  The welcome sequence runs once per process; restart the script to run it again.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 7.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2  # update noise floor only when below floor * this
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# Cursor (Windows): bring an existing Cursor window to the front on double clap.
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = True
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = True

PRIME_WELCOME_ENABLED = True
PRIME_WELCOME_PHRASE = "Welcome back, sir. Prime is online and ready."
# Short pause before speaking.
PRIME_WELCOME_DELAY_S = 0.5
# Save ElevenLabs PCM as WAV under .cache/prime_welcome/; replay skips the API when the key matches.
PRIME_WELCOME_CACHE_ENABLED = True

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env")
load_dotenv(_HERE / "api.env")

# Optional UI hook: called with True/False while Prime is speaking (set by the UI).
SPEAKING_HOOK = None

# Speak Prime's chat replies aloud (ElevenLabs voice). Set PRIME_SPEAK_REPLIES=0 to mute.
SPEAK_REPLIES = (os.environ.get("PRIME_SPEAK_REPLIES", "1").strip() != "0")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("prime")


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("PRIME_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set PRIME_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using PRIME_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check Windows input level or try another PRIME_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _prime_welcome_cache_dir() -> Path:
    base = Path(__file__).resolve().parent
    override = (os.environ.get("PRIME_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "prime_welcome"


def _prime_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _prime_welcome_cache_dir() / f"{digest}.wav"


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play cached welcome audio: %s", e)
        return False
    return True


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def speak_text(text: str, use_cache: bool = False) -> None:
    """Speak `text` with the ElevenLabs voice (blocking). Welcome lines are cached as WAV."""
    text = text.strip()
    if not text:
        return
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not vid:
        log.warning("Set ELEVENLABS_VOICE_ID in the environment for ElevenLabs TTS.")
        return

    cache_path = _prime_welcome_cache_path(text, vid, model_id, output_format)
    if use_cache and cache_path.is_file():
        log.info("Playing from cache: %s", cache_path)
        if _play_pcm_wav_file(cache_path):
            return
        log.warning("Cache miss after read failure; fetching from ElevenLabs.")

    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        log.warning("Set ELEVENLABS_API_KEY in the environment for ElevenLabs TTS.")
        return
    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        log.warning("Install dependencies: pip install -r requirements.txt")
        return
    try:
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid,
            text=text,
            model_id=model_id,
            output_format=output_format,
        )
        raw = b"".join(chunks)
    except Exception as e:
        log.warning("ElevenLabs TTS failed: %s", e)
        return
    if not raw:
        log.warning("ElevenLabs returned empty audio.")
        return
    if use_cache:
        try:
            _save_pcm_wav_file(cache_path, raw, pcm_rate)
            log.info("Saved audio to cache: %s", cache_path)
        except OSError as e:
            log.warning("Could not save cache: %s", e)
    pcm_f = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, pcm_rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play ElevenLabs audio: %s", e)


def say_prime_welcome() -> None:
    if not PRIME_WELCOME_ENABLED or not PRIME_WELCOME_PHRASE.strip():
        return
    speak_text(PRIME_WELCOME_PHRASE, use_cache=PRIME_WELCOME_CACHE_ENABLED)


def _cursor_executable() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in ("Programs\\cursor\\Cursor.exe", "Programs\\Cursor\\Cursor.exe"):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    return shutil.which("cursor")


def _cursor_largest_main_hwnd_win32() -> int | None:
    """Largest top-level Cursor.exe window (visible or minimized)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    candidates: list[tuple[int, wintypes.HWND]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "cursor.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    if not candidates:
        return None
    return int(max(candidates, key=lambda t: t[0])[1])


def _cursor_foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _cursor_send_f11_fullscreen_win32(hwnd: int) -> None:
    """F11 toggles Zen/fullscreen in Cursor (Electron)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _cursor_foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _focus_existing_cursor_window_win32() -> bool:
    """Bring an existing Cursor.exe main window to the foreground (no new process)."""
    if sys.platform != "win32":
        return False
    hwnd = _cursor_largest_main_hwnd_win32()
    if hwnd is None:
        return False
    _cursor_foreground_hwnd_win32(hwnd)
    return True


def _welcome_with_hooks() -> None:
    if SPEAKING_HOOK:
        SPEAKING_HOOK(True)
    try:
        say_prime_welcome()
    finally:
        if SPEAKING_HOOK:
            SPEAKING_HOOK(False)


def run_double_clap_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    if PRIME_WELCOME_ENABLED and PRIME_WELCOME_PHRASE.strip():
        delay = max(0.0, PRIME_WELCOME_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=_welcome_with_hooks, daemon=True).start()
    open_cursor_window()


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP and not OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        return
    exe = _cursor_executable()
    if not exe:
        log.warning(
            "Could not find Cursor (install app or add the `cursor` command to PATH)."
        )
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            focused = (
                sys.platform == "win32" and _focus_existing_cursor_window_win32()
            )
            if not focused:
                subprocess.Popen([exe], **popen_kw)
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **popen_kw)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if sys.platform == "win32" and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        hwnd = _cursor_largest_main_hwnd_win32()
        if hwnd is not None:
            _cursor_send_f11_fullscreen_win32(hwnd)
        else:
            log.warning("Cursor fullscreen: no Cursor window found to send F11.")


def listen_for_claps(on_double_clap=None, stop_event=None) -> int:
    """Mic loop. on_double_clap(first: bool) is called on every double clap (UI mode)."""
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    welcome_sequence_done = False

    log.info(
        "Listening (double clap: %.2f–%.2fs apart, rate=%d, block=%d ms, "
        "spike_ratio=%.1f, cooldown=%.2fs). Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SAMPLE_RATE,
        BLOCK_MS,
        SPIKE_RATIO,
        COOLDOWN_S,
    )
    if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
        log.info(
            "Double clap will foreground an existing Cursor window (Windows API); "
            "falls back to launching Cursor if none is running."
        )
    if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        log.info("Double clap will also open a new Cursor window (-n).")
    if CURSOR_OPEN_FULLSCREEN and sys.platform == "win32":
        log.info("Cursor will be sent F11 for fullscreen after focus/launch.")
    if PRIME_WELCOME_ENABLED:
        ev, em, ef, er = elevenlabs_env_config()
        log.info(
            "On double clap, after %.2fs: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
            PRIME_WELCOME_DELAY_S,
            PRIME_WELCOME_PHRASE.strip(),
            ev or "(unset)",
            em,
            ef,
            er,
        )

    input_idx = _choose_input_device(blocksize)

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while stop_event is None or not stop_event.is_set():
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (
                        1.0 - NOISE_FLOOR_ALPHA
                    ) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                    else:
                        gap = now - first_clap_time
                        if gap < MIN_DOUBLE_GAP_S:
                            pass
                        elif gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            if on_double_clap is not None:
                                on_double_clap(not welcome_sequence_done)
                            if not welcome_sequence_done:
                                welcome_sequence_done = True
                                log.info(
                                    "Double clap detected (gap=%.3fs, rms=%.5f, "
                                    "noise_floor=%.5f, threshold=%.5f) — running welcome once",
                                    gap,
                                    level,
                                    noise_floor,
                                    threshold,
                                )
                                threading.Thread(
                                    target=run_double_clap_actions, daemon=True
                                ).start()
                        else:
                            first_clap_time = now

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("If PortAudio fails, install/repair drivers or try another SAMPLE_RATE.")
        return 1

    return 0


def run_ui() -> int:
    """Floating orb + HUD window. Double clap (or clicking the orb) opens Prime."""
    import signal

    from PySide6.QtCore import QObject, QTimer, Signal

    import prime_ui

    global SPEAKING_HOOK
    app = prime_ui.make_app(sys.argv)
    user_name = (os.environ.get("PRIME_USER_NAME") or "Spidy").strip() or "Spidy"
    orb = prime_ui.FloatingOrb()
    win = prime_ui.PrimeWindow(user_name)

    class Bridge(QObject):
        clap = Signal(bool)
        speaking = Signal(bool)
        chunk = Signal(int, str)
        done = Signal(int, str)
        acting = Signal(bool)
        log_line = Signal(str)
        confirm = Signal(str)
        hide_ui = Signal()

    bridge = Bridge()

    def open_prime() -> None:
        orb.hide()
        win.show_and_focus()

    def close_to_orb() -> None:
        win.hide()
        orb.show()

    flags = {"speaking": False, "acting": False}

    def refresh_state() -> None:
        st = "acting" if flags["acting"] else ("speaking" if flags["speaking"] else "idle")
        win.set_state(st)
        orb.set_state(st)

    def on_speaking(on: bool) -> None:
        flags["speaking"] = on
        refresh_state()

    def on_acting(on: bool) -> None:
        flags["acting"] = on
        refresh_state()

    conf = {"event": threading.Event(), "ok": False}

    def on_confirm_answer(ok: bool) -> None:
        conf["ok"] = ok
        conf["event"].set()

    def stop_all() -> None:
        c = req.get("cancel")
        if c is not None:
            c.set()
        ab = req.get("abort")
        if ab is not None:
            ab.set()
        conf["ok"] = False
        conf["event"].set()  # releases a pending ALLOW / DENY prompt
        win.confirm.hide()
        if req.get("msg") is not None and not req["text"]:
            req["msg"].set_text("(stopped)")
        req["id"] += 1  # ignore anything still in flight
        try:
            sd.stop()
        except Exception:
            pass
        flags["speaking"] = False
        flags["acting"] = False
        refresh_state()

    from brain import Brain
    import actions

    brain = Brain()
    win.set_link(brain.available())
    win.set_voice(SPEAK_REPLIES)
    req: dict = {"id": 0, "cancel": None, "abort": None, "msg": None, "text": ""}

    def run_actions(acts: list, abort: threading.Event) -> None:
        """Runs in a worker thread: executes the agent's action tags one by one."""
        bridge.acting.emit(True)
        hidden = False
        try:
            for a in acts:
                if abort.is_set():
                    break
                desc = actions.describe(a)
                if actions.needs_confirm(a):
                    conf["event"].clear()
                    conf["ok"] = False
                    bridge.confirm.emit(desc)
                    conf["event"].wait(timeout=60)
                    if not conf["ok"] or abort.is_set():
                        bridge.log_line.emit(f"✗ DENIED · {desc}")
                        continue
                if actions.takes_input(a) and not hidden:
                    hidden = True
                    bridge.hide_ui.emit()  # keystrokes must reach the target app, not Prime
                    time.sleep(0.5)
                bridge.log_line.emit(f"▸ {desc}")
                try:
                    res = actions.run(a, abort)
                    bridge.log_line.emit(f"✓ {res}")
                except actions.ActionError as e:
                    bridge.log_line.emit(f"✗ {e}")
                except Exception as e:  # noqa: BLE001
                    bridge.log_line.emit(f"✗ {e.__class__.__name__}: {str(e)[:120]}")
        finally:
            bridge.acting.emit(False)

    def on_submit(text: str) -> None:
        if not brain.available():
            win.add_message(
                "assistant",
                "I need ELEVENLABS_API_KEY in api.env to think. Add it and restart me.",
            )
            return
        req["id"] += 1
        rid = req["id"]
        cancel = threading.Event()
        abort = threading.Event()
        req.update(cancel=cancel, abort=abort, text="", msg=win.add_message("assistant", "…"))
        win.set_state("thinking")
        orb.set_state("thinking")

        def work() -> None:
            err = ""
            shown = ""
            strip = actions.TagStripper()
            try:
                for piece in brain.stream(text, cancel):
                    vis = strip.feed(piece)
                    if vis:
                        shown += vis
                        bridge.chunk.emit(rid, vis)
                tail = strip.flush()
                if tail:
                    shown += tail
                    bridge.chunk.emit(rid, tail)
            except Exception as e:  # noqa: BLE001
                err = str(e)[:200] or e.__class__.__name__
            acts = [] if (err or cancel.is_set()) else actions.parse_actions(strip.tags)
            if acts and not shown.strip():
                shown = "On it."
                bridge.chunk.emit(rid, shown)
            bridge.done.emit(rid, err)
            if acts:
                threading.Thread(target=run_actions, args=(acts, abort), daemon=True).start()
            if not err and not cancel.is_set() and SPEAK_REPLIES and shown.strip():
                bridge.speaking.emit(True)
                try:
                    speak_text(shown.strip())
                finally:
                    bridge.speaking.emit(False)

        threading.Thread(target=work, daemon=True).start()

    def on_chunk(rid: int, piece: str) -> None:
        if rid != req["id"]:
            return
        req["text"] += piece
        req["msg"].set_text(req["text"].lstrip())
        win.transcript.scroll_to_end()
        if win.state != "replying":
            win.set_state("replying")
            orb.set_state("replying")

    def on_done(rid: int, err: str) -> None:
        if rid != req["id"]:
            return
        if err and not req["text"]:
            req["msg"].set_text(f"Sorry, my brain didn't respond: {err}")
        elif not req["text"].strip():
            req["msg"].set_text("(no reply)")
        else:
            req["msg"].set_text(req["text"].strip())
        refresh_state()

    def on_log_line(line: str) -> None:
        win.add_action_line(line)

    def on_confirm_request(desc: str) -> None:
        orb.hide()
        win.show_confirm(desc)

    def on_orb_pressed() -> None:
        nxt = "idle" if win.state == "listening" else "listening"
        win.set_state(nxt)
        orb.set_state(nxt)

    bridge.clap.connect(lambda _first: open_prime())
    bridge.speaking.connect(on_speaking)
    bridge.chunk.connect(on_chunk)
    bridge.done.connect(on_done)
    bridge.acting.connect(on_acting)
    bridge.log_line.connect(on_log_line)
    bridge.confirm.connect(on_confirm_request)
    bridge.hide_ui.connect(close_to_orb)
    win.confirm_answered.connect(on_confirm_answer)
    SPEAKING_HOOK = bridge.speaking.emit

    orb.activated.connect(open_prime)
    orb.open_requested.connect(open_prime)
    orb.quit_requested.connect(app.quit)
    win.hide_requested.connect(close_to_orb)
    win.quit_requested.connect(app.quit)
    win.stop_requested.connect(stop_all)
    win.submitted.connect(on_submit)
    win.new_chat_requested.connect(brain.reset)
    win.orb_pressed.connect(on_orb_pressed)

    stop = threading.Event()

    def clap_worker() -> None:
        try:
            listen_for_claps(on_double_clap=bridge.clap.emit, stop_event=stop)
        except SystemExit:
            log.error("Clap listener stopped (no usable microphone). The orb still works.")

    threading.Thread(target=clap_worker, daemon=True).start()

    signal.signal(signal.SIGINT, lambda *_: app.quit())
    keepalive = QTimer()
    keepalive.start(250)  # lets Python handle Ctrl+C while Qt runs
    keepalive.timeout.connect(lambda: None)

    orb.show()
    log.info("Prime is running. Double clap or click the orb to open. Ctrl+C or right-click the orb to quit.")
    code = app.exec()
    stop.set()
    return code


def main() -> int:
    if "--no-ui" in sys.argv:
        return listen_for_claps()
    return run_ui()


if __name__ == "__main__":
    sys.exit(main())
