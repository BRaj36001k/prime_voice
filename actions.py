"""
Prime desktop control (Windows).

The ElevenLabs agent can't touch your PC by itself, so Prime teaches it a tiny protocol: when you ask
for something, the reply ends with action tags such as

    Opening Chrome.
    [[ACTION {"tool":"open_app","app":"chrome"}]]

prime.py hides the tags from the chat, then runs them here, one by one.

Safety
  - close_app, command and system (shutdown / restart / sleep / lock) always ask you to ALLOW first.
  - The Stop button aborts the queue; slamming the mouse into a screen corner aborts too (pyautogui failsafe).
  - Prime has no screen vision yet, so it controls the PC mostly through apps, URLs and keyboard shortcuts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

log = logging.getLogger("prime")


class ActionError(Exception):
    pass


# --- protocol text, sent to the agent as part of its prompt ----------------------
PROTOCOL_PROMPT = """You can operate Spidy's Windows PC. To do something on the PC, end your reply with one action tag per step, each on its own line, exactly like this:
[[ACTION {"tool":"open_app","app":"chrome"}]]
Tools:
- open_app {"app": name of any installed app, e.g. chrome, edge, notepad, calculator, spotify, vs code, whatsapp, settings}
- open_url {"url": address}
- search {"query": text, "site": optional "google" (default) or "youtube"}
- open_path {"path": file or folder path}
- close_app {"app": name}
- type {"text": text to type into the focused window}
- keys {"combo": "ctrl+c" or "enter" or "win+d" or "alt+tab"}
- click {"x": px, "y": px, "button": "left|right", "clicks": 1 or 2}  (omit x and y to click where the cursor is)
- move {"x": px, "y": px}
- scroll {"amount": positive = up, negative = down}
- drag {"x1":..., "y1":..., "x2":..., "y2":...}
- volume {"direction": "up|down|mute", "steps": number}
- media {"op": "playpause|next|prev"}
- window {"op": "switch|desktop|maximize|minimize|snap_left|snap_right|task_view"}
- screenshot {}
- system {"op": "lock|sleep|shutdown|restart|cancel_shutdown"}
- command {"cmd": a Windows shell command}
- wait {"seconds": 1-10}
Rules for actions: only act when asked. Use several tags in order for multi-step jobs (e.g. open_app, wait, type). Say one short spoken sentence before the tags, never describe the tags themselves, and never claim you did something unless you emitted its tag. You cannot see the screen, so do not guess pixel coordinates; prefer shortcuts and keys. close_app, command and system are confirmed by Spidy on screen before they run."""


# --- tag parsing -------------------------------------------------------------------
class TagStripper:
    """Feed streamed text; get back the visible text with [[...]] tags removed (even if split across chunks)."""

    def __init__(self) -> None:
        self.buf = ""
        self.tags: list[str] = []

    def feed(self, s: str) -> str:
        self.buf += s
        out = ""
        while True:
            i = self.buf.find("[[")
            if i == -1:
                keep = 1 if self.buf.endswith("[") else 0
                cut = len(self.buf) - keep
                out += self.buf[:cut]
                self.buf = self.buf[cut:]
                break
            out += self.buf[:i]
            self.buf = self.buf[i:]
            j = self.buf.find("]]")
            if j == -1:
                break
            self.tags.append(self.buf[2:j])
            self.buf = self.buf[j + 2:]
        return out

    def flush(self) -> str:
        rest, self.buf = self.buf, ""
        return "" if rest.startswith("[[") else rest


def parse_actions(tags: list[str]) -> list[dict]:
    acts: list[dict] = []
    for t in tags:
        t = t.strip()
        if not t.upper().startswith("ACTION"):
            continue
        body = t[6:].lstrip(" :")
        try:
            obj = json.loads(body)
        except ValueError:
            m = re.search(r"\{.*\}", body, re.S)
            try:
                obj = json.loads(m.group(0)) if m else None
            except ValueError:
                obj = None
        if isinstance(obj, dict) and obj.get("tool") in SPEC:
            acts.append(obj)
        else:
            log.warning("Ignored bad action tag: %r", t[:120])
    return acts


# tool -> (needs_confirm, takes_over_input)
SPEC: dict[str, tuple[bool, bool]] = {
    "open_app": (False, False),
    "open_url": (False, False),
    "search": (False, False),
    "open_path": (False, False),
    "close_app": (True, False),
    "type": (False, True),
    "keys": (False, True),
    "click": (False, True),
    "move": (False, True),
    "scroll": (False, True),
    "drag": (False, True),
    "volume": (False, False),
    "media": (False, False),
    "window": (False, True),
    "screenshot": (False, False),
    "system": (True, False),
    "command": (True, False),
    "wait": (False, False),
}


def needs_confirm(a: dict) -> bool:
    if a.get("tool") == "system" and a.get("op") == "cancel_shutdown":
        return False
    return SPEC[a["tool"]][0]


def takes_input(a: dict) -> bool:
    return SPEC[a["tool"]][1]


def describe(a: dict) -> str:
    t = a["tool"]
    g = lambda k: str(a.get(k, "")).strip()  # noqa: E731
    short = lambda s: (s if len(s) <= 60 else s[:57] + "…")  # noqa: E731
    if t == "open_app":
        return f"OPEN APP · {g('app')}"
    if t == "close_app":
        return f"CLOSE APP · {g('app')}"
    if t == "open_url":
        return f"OPEN URL · {short(g('url'))}"
    if t == "search":
        return f"SEARCH {(g('site') or 'google').upper()} · {short(g('query'))}"
    if t == "open_path":
        return f"OPEN · {short(g('path'))}"
    if t == "type":
        return f"TYPE · {short(g('text'))}"
    if t == "keys":
        return f"KEYS · {g('combo')}"
    if t == "click":
        where = f"({g('x')}, {g('y')})" if "x" in a and "y" in a else "at cursor"
        return f"CLICK · {g('button') or 'left'} {where}"
    if t == "move":
        return f"MOVE CURSOR · ({g('x')}, {g('y')})"
    if t == "scroll":
        return f"SCROLL · {g('amount')}"
    if t == "drag":
        return f"DRAG · ({g('x1')}, {g('y1')}) → ({g('x2')}, {g('y2')})"
    if t == "volume":
        return f"VOLUME · {g('direction')}"
    if t == "media":
        return f"MEDIA · {g('op')}"
    if t == "window":
        return f"WINDOW · {g('op')}"
    if t == "screenshot":
        return "SCREENSHOT"
    if t == "system":
        return f"SYSTEM · {g('op').upper()}"
    if t == "command":
        return f"RUN COMMAND · {short(g('cmd'))}"
    if t == "wait":
        return f"WAIT · {g('seconds')}s"
    return t.upper()


# --- helpers ---------------------------------------------------------------------------
def _win() -> bool:
    return sys.platform == "win32"


def _need_win() -> None:
    if not _win():
        raise ActionError("Desktop control works on Windows only.")


NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

DIRECT = {  # spoken name -> something Windows can launch directly
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "command prompt": "cmd.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "task manager": "taskmgr.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "files": "explorer.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
    "snipping tool": "snippingtool.exe",
}
QUERY_HINT = {  # spoken name -> better Start-menu search text
    "chrome": "google chrome",
    "edge": "microsoft edge",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "code": "visual studio code",
    "word": "word",
    "excel": "excel",
    "powerpoint": "powerpoint",
    "ppt": "powerpoint",
    "store": "microsoft store",
}
EXE_FOR_CLOSE = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "notepad": "notepad.exe", "calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe",
    "spotify": "Spotify.exe", "vscode": "Code.exe", "vs code": "Code.exe", "code": "Code.exe", "cursor": "Cursor.exe",
    "word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE", "paint": "mspaint.exe",
    "task manager": "Taskmgr.exe", "whatsapp": "WhatsApp.exe", "teams": "ms-teams.exe", "vlc": "vlc.exe",
}

_apps_cache: list[dict] | None = None
_apps_lock = threading.Lock()


def _start_apps() -> list[dict]:
    """Installed apps from the Start menu: [{'Name':..., 'AppID':...}, ...] (cached)."""
    global _apps_cache
    with _apps_lock:
        if _apps_cache is not None:
            return _apps_cache
        cmd = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
               "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                               capture_output=True, text=True, encoding="utf-8", timeout=25,
                               creationflags=NO_WINDOW)
            data = json.loads(r.stdout) if r.stdout.strip() else []
        except Exception as e:  # noqa: BLE001
            log.warning("Could not list Start apps: %s", e)
            data = []
        if isinstance(data, dict):
            data = [data]
        _apps_cache = [d for d in data if isinstance(d, dict) and d.get("Name") and d.get("AppID")]
        return _apps_cache


def match_app(query: str, apps: list[dict]) -> dict | None:
    q = query.lower().strip()
    toks = [t for t in re.split(r"\W+", q) if t]
    best, best_score = None, 0
    for a in apps:
        n = a["Name"].lower()
        if n == q:
            s = 100
        elif n.startswith(q):
            s = 85
        elif q in n:
            s = 70
        elif toks and all(t in n for t in toks):
            s = 55
        else:
            continue
        s -= min(len(n), 60) * 0.1  # prefer shorter names ("Chrome" over "Chrome Remote Desktop")
        if s > best_score:
            best, best_score = a, s
    return best


def open_app(name: str) -> str:
    _need_win()
    key = name.lower().strip()
    if not key:
        raise ActionError("No app name given.")
    if key in DIRECT:
        os.startfile(DIRECT[key])  # type: ignore[attr-defined]
        return f"Opened {name}"
    hit = match_app(QUERY_HINT.get(key, key), _start_apps())
    if hit is None and key in QUERY_HINT:
        hit = match_app(key, _start_apps())
    if hit:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{hit['AppID']}"], creationflags=NO_WINDOW)
        return f"Opened {hit['Name']}"
    exe = shutil.which(key) or shutil.which(key + ".exe")
    if exe:
        os.startfile(exe)  # type: ignore[attr-defined]
        return f"Opened {name}"
    raise ActionError(f"Couldn't find an app called '{name}'.")


def close_app(name: str) -> str:
    _need_win()
    key = name.lower().strip()
    exe = EXE_FOR_CLOSE.get(key) or (key if key.endswith(".exe") else key.replace(" ", "") + ".exe")
    r = subprocess.run(["taskkill", "/IM", exe], capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise ActionError(f"'{name}' isn't running (or couldn't be closed).")
    return f"Closed {name}"


def _gui():
    try:
        import pyautogui
    except Exception as e:  # noqa: BLE001
        raise ActionError("Install pyautogui: pip install -r requirements.txt") from e
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "windows": "win", "super": "win", "cmd": "win", "return": "enter",
    "escape": "esc", "del": "delete", "pageup": "pageup", "page up": "pageup", "page down": "pagedown",
    "spacebar": "space", "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}


def normalize_combo(combo: str) -> list[str]:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", combo.strip()) if p.strip()]
    if not parts:
        raise ActionError("No keys given.")
    return [KEY_ALIASES.get(p, p) for p in parts]


def type_text(text: str) -> str:
    gui = _gui()
    if text.isascii():
        gui.write(text, interval=0.012)
    else:  # Tamil, Hindi, Malayalam, emoji ... go through the clipboard
        try:
            import pyperclip
        except Exception as e:  # noqa: BLE001
            raise ActionError("Install pyperclip to type non-English text.") from e
        old = None
        try:
            old = pyperclip.paste()
        except Exception:  # noqa: BLE001
            pass
        pyperclip.copy(text)
        time.sleep(0.05)
        gui.hotkey("ctrl", "v")
        time.sleep(0.15)
        if old is not None:
            try:
                pyperclip.copy(old)
            except Exception:  # noqa: BLE001
                pass
    return "Typed"


def _num(a: dict, k: str, default: float | None = None) -> float:
    try:
        return float(a[k])
    except (KeyError, TypeError, ValueError):
        if default is not None:
            return default
        raise ActionError(f"Missing number '{k}'.") from None


WINDOW_OPS = {
    "switch": ("alt", "tab"), "desktop": ("win", "d"), "maximize": ("win", "up"), "minimize": ("win", "down"),
    "snap_left": ("win", "left"), "snap_right": ("win", "right"), "task_view": ("win", "tab"),
}
MEDIA_KEYS = {"playpause": "playpause", "play": "playpause", "pause": "playpause", "next": "nexttrack",
              "prev": "prevtrack", "previous": "prevtrack"}


def _sleep(sec: float, abort: threading.Event) -> None:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if abort.is_set():
            raise ActionError("Stopped.")
        time.sleep(0.05)


def run(a: dict, abort: threading.Event) -> str:
    """Execute one action. Returns a short result line; raises ActionError on failure."""
    t = a["tool"]
    s = lambda k: str(a.get(k, "")).strip()  # noqa: E731
    if abort.is_set():
        raise ActionError("Stopped.")
    if t == "open_app":
        return open_app(s("app"))
    if t == "close_app":
        return close_app(s("app"))
    if t == "open_url":
        url = s("url")
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened {url}"
    if t == "search":
        q = quote_plus(s("query"))
        site = (s("site") or "google").lower()
        url = (f"https://www.youtube.com/results?search_query={q}" if site == "youtube"
               else f"https://www.google.com/search?q={q}")
        webbrowser.open(url)
        return f"Searched {site} for '{s('query')}'"
    if t == "open_path":
        _need_win()
        p = Path(os.path.expandvars(os.path.expanduser(s("path"))))
        if not p.exists():
            raise ActionError(f"Path not found: {p}")
        os.startfile(str(p))  # type: ignore[attr-defined]
        return f"Opened {p.name or p}"
    if t == "wait":
        _sleep(max(0.0, min(10.0, _num(a, "seconds", 1.0))), abort)
        return "Waited"
    if t == "command":
        _need_win()
        r = subprocess.run(s("cmd"), shell=True, capture_output=True, text=True, timeout=30,
                           creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip().replace("\n", " ")[:200]
        return f"Exit {r.returncode}" + (f" · {out}" if out else "")
    if t == "system":
        _need_win()
        op = s("op").lower()
        cmds = {
            "lock": ["rundll32.exe", "user32.dll,LockWorkStation"],
            "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            "shutdown": ["shutdown", "/s", "/t", "15"],
            "restart": ["shutdown", "/r", "/t", "15"],
            "cancel_shutdown": ["shutdown", "/a"],
        }
        if op not in cmds:
            raise ActionError(f"Unknown system op '{op}'.")
        subprocess.run(cmds[op], creationflags=NO_WINDOW)
        return {"shutdown": "Shutting down in 15s (say 'cancel shutdown' to stop)",
                "restart": "Restarting in 15s (say 'cancel shutdown' to stop)"}.get(op, f"Done: {op}")

    gui = _gui()  # everything below drives the keyboard / mouse / screen
    try:
        if t == "type":
            return type_text(s("text"))
        if t == "keys":
            keys = normalize_combo(s("combo"))
            gui.hotkey(*keys) if len(keys) > 1 else gui.press(keys[0])
            return f"Pressed {'+'.join(keys)}"
        if t == "move":
            gui.moveTo(_num(a, "x"), _num(a, "y"), duration=0.3)
            return "Cursor moved"
        if t == "click":
            btn = (s("button") or "left").lower()
            n = int(_num(a, "clicks", 1))
            if "x" in a and "y" in a:
                gui.click(_num(a, "x"), _num(a, "y"), clicks=n, button=btn, interval=0.1, duration=0.25)
            else:
                gui.click(clicks=n, button=btn, interval=0.1)
            return "Clicked"
        if t == "scroll":
            gui.scroll(int(_num(a, "amount") * 120 if abs(_num(a, "amount")) < 20 else _num(a, "amount")))
            return "Scrolled"
        if t == "drag":
            gui.moveTo(_num(a, "x1"), _num(a, "y1"), duration=0.25)
            gui.dragTo(_num(a, "x2"), _num(a, "y2"), duration=0.5, button="left")
            return "Dragged"
        if t == "volume":
            d = s("direction").lower()
            if d == "mute":
                gui.press("volumemute")
                return "Volume mute toggled"
            key = "volumeup" if d == "up" else "volumedown"
            n = max(1, min(25, int(_num(a, "steps", 5))))
            gui.press(key, presses=n, interval=0.02)
            return f"Volume {d} x{n}"
        if t == "media":
            key = MEDIA_KEYS.get(s("op").lower())
            if not key:
                raise ActionError(f"Unknown media op '{s('op')}'.")
            gui.press(key)
            return f"Media {s('op')}"
        if t == "window":
            combo = WINDOW_OPS.get(s("op").lower())
            if not combo:
                raise ActionError(f"Unknown window op '{s('op')}'.")
            gui.hotkey(*combo)
            return f"Window {s('op')}"
        if t == "screenshot":
            folder = Path.home() / "Pictures" / "Prime Screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / time.strftime("prime_%Y%m%d_%H%M%S.png")
            gui.screenshot(str(path))
            return f"Saved {path}"
    except ActionError:
        raise
    except Exception as e:  # noqa: BLE001
        if e.__class__.__name__ == "FailSafeException":
            abort.set()
            raise ActionError("Failsafe: cursor hit a screen corner, stopped.") from e
        raise ActionError(str(e) or e.__class__.__name__) from e
    raise ActionError(f"Unknown tool '{t}'.")
