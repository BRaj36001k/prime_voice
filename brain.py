"""
Prime brain: chats through the ElevenLabs Agents platform (text-only), no other AI API needed.

How it works
  1. On first use Prime creates a text-only ElevenLabs agent called "Prime" (its LLM + persona live
     on ElevenLabs) and remembers its id in `.cache/prime_agent.json`. Or set ELEVENLABS_AGENT_ID
     to use an agent you made in the dashboard.
  2. Each chat is one WebSocket session to that agent, so ElevenLabs keeps the conversation memory.
  3. Replies stream back as text chunks; set the cancel Event to stop one early.

Env (api.env / .env)
  ELEVENLABS_API_KEY   required (the key needs the "ElevenLabs Agents" permission to create an agent)
  ELEVENLABS_VOICE_ID  voice stored on the agent
  ELEVENLABS_AGENT_ID  optional, use an existing agent instead of auto-creating one
  PRIME_LLM            optional, default gemini-2.5-flash (e.g. claude-sonnet-4-5, gemini-2.5-flash-lite)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

log = logging.getLogger("prime")

API = "https://api.elevenlabs.io"
CACHE = Path(__file__).resolve().parent / ".cache" / "prime_agent.json"
DEFAULT_LLM = "gemini-2.5-flash"

SYSTEM_PROMPT = (
    "You are Prime, a personal voice assistant for Spidy, an Electrical and Electronics "
    "Engineering student who works on embedded systems, IoT, PCB design and power systems. "
    "Reply in the language the user wrote in (English, Tamil, Tanglish, Malayalam or Hindi), "
    "in a natural, direct tone. Keep answers short and conversational unless asked for detail, "
    "because replies will be spoken aloud. Avoid markdown, tables, bullet lists and emojis."
)
try:
    from actions import PROTOCOL_PROMPT
except Exception:  # noqa: BLE001
    PROTOCOL_PROMPT = ""
SYSTEM_PROMPT = SYSTEM_PROMPT + ("\n\n" + PROTOCOL_PROMPT if PROTOCOL_PROMPT else "")
CLIENT_EVENTS = ["agent_response", "agent_chat_response_part", "user_transcript", "ping"]
TURN_TIMEOUT_S = 60.0
POST_STOP_WAIT_S = 1.5
DRAIN_QUIET_S = 1.2


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class BrainError(RuntimeError):
    pass


def _http(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"xi-api-key": _env("ELEVENLABS_API_KEY"), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:  # noqa: BLE001
            pass
        if e.code in (401, 403):
            raise BrainError(
                "ElevenLabs rejected the key. In the ElevenLabs dashboard, enable the "
                "'ElevenLabs Agents' permission on this API key (or set ELEVENLABS_AGENT_ID)."
            ) from e
        raise BrainError(f"ElevenLabs HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise BrainError(f"Can't reach ElevenLabs: {e.reason}") from e


def _agent_config(llm: str) -> dict:
    return {
        "agent": {
            "first_message": "",
            "language": "en",
            "prompt": {"prompt": SYSTEM_PROMPT, "llm": llm},
        },
        "tts": {"voice_id": _env("ELEVENLABS_VOICE_ID") or "IKne3meq5aSn9XLyUdCD"},
        "conversation": {"text_only": True, "client_events": CLIENT_EVENTS},
    }


def _fingerprint(llm: str) -> str:
    return hashlib.sha256(f"{llm}|{SYSTEM_PROMPT}".encode()).hexdigest()[:16]


def ensure_agent() -> str:
    """Return the agent id (env, cached, updated, or newly created)."""
    if _env("ELEVENLABS_AGENT_ID"):
        return _env("ELEVENLABS_AGENT_ID")
    llm = _env("PRIME_LLM") or DEFAULT_LLM
    fp = _fingerprint(llm)
    cached: dict = {}
    try:
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    aid = cached.get("agent_id")
    if aid and cached.get("fp") == fp:
        return aid
    if aid:  # settings changed: update in place, else fall through and recreate
        try:
            _http("PATCH", f"/v1/convai/agents/{aid}", {"conversation_config": _agent_config(llm)})
            _save_cache(aid, fp)
            log.info("Updated ElevenLabs agent %s (llm=%s)", aid, llm)
            return aid
        except BrainError as e:
            log.warning("Agent update failed (%s); creating a new one", e)
    body = {"name": "Prime", "conversation_config": _agent_config(llm)}
    try:
        out = _http("POST", "/v1/convai/agents/create", body)
    except BrainError as e:
        if "HTTP 422" not in str(e):
            raise
        log.warning("Agent create rejected client_events; retrying without them")
        body["conversation_config"]["conversation"].pop("client_events")
        out = _http("POST", "/v1/convai/agents/create", body)
    aid = out.get("agent_id")
    if not aid:
        raise BrainError("ElevenLabs did not return an agent id.")
    _save_cache(aid, fp)
    log.info("Created ElevenLabs agent %s (llm=%s)", aid, llm)
    return aid


def _save_cache(aid: str, fp: str) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"agent_id": aid, "fp": fp}), encoding="utf-8")
    except OSError as e:
        log.warning("Could not cache agent id: %s", e)


class _Session:
    """One WebSocket conversation. A reader thread answers pings and queues events."""

    def __init__(self, agent_id: str) -> None:
        from websockets.sync.client import connect

        url = _http("GET", f"/v1/convai/conversation/get-signed-url?agent_id={agent_id}")["signed_url"]
        self.ws = connect(url, open_timeout=15, max_size=None)
        self.q: queue.Queue = queue.Queue()
        self.closed = False
        self.dirty = False  # last turn was cancelled; its tail may still be arriving
        self.ws.send(json.dumps({"type": "conversation_initiation_client_data"}))
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        reason = "connection closed"
        try:
            for raw in self.ws:
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if ev.get("type") == "ping":
                    info = ev.get("ping_event") or {}
                    delay = (info.get("ping_ms") or 0) / 1000.0
                    threading.Timer(delay, self._send, ({"type": "pong", "event_id": info.get("event_id")},)).start()
                else:
                    self.q.put(ev)
        except Exception as e:  # noqa: BLE001
            reason = str(e) or reason
        self.closed = True
        self.q.put({"type": "_closed", "reason": reason})

    def _send(self, obj: dict) -> None:
        try:
            self.ws.send(json.dumps(obj))
        except Exception:  # noqa: BLE001
            pass

    def ask(self, text: str, cancel: threading.Event) -> Iterator[str]:
        if self.dirty:  # let a cancelled reply finish so its tail doesn't leak into this turn
            self.dirty = False
            quiet = 0.0
            while quiet < DRAIN_QUIET_S and not self.closed:
                try:
                    ev = self.q.get(timeout=0.2)
                except queue.Empty:
                    quiet += 0.2
                    continue
                quiet = 0.0
                if ev.get("type") in ("agent_response", "_closed"):
                    break
        while not self.q.empty():  # drop any other leftover events
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.ws.send(json.dumps({"type": "user_message", "text": text}))
        streamed = False
        stopped = False
        waited = 0.0
        while True:
            if cancel.is_set():
                self.dirty = True
                return
            try:
                ev = self.q.get(timeout=0.25)
            except queue.Empty:
                waited += 0.25
                if (stopped and waited >= POST_STOP_WAIT_S) or waited >= TURN_TIMEOUT_S:
                    return
                continue
            waited = 0.0 if not stopped else waited
            t = ev.get("type")
            if t == "agent_chat_response_part":
                part = ev.get("text_response_part") or {}
                if part.get("type") == "delta" and part.get("text"):
                    streamed = True
                    yield part["text"]
                elif part.get("type") == "stop":
                    stopped = True
            elif t == "agent_response":
                if not streamed:
                    txt = (ev.get("agent_response_event") or {}).get("agent_response") or ""
                    if txt:
                        yield txt
                return
            elif t == "_closed":
                if streamed:
                    return
                raise BrainError(f"ElevenLabs closed the chat: {ev.get('reason')}")
            elif t in ("error", "conversation_error"):
                raise BrainError(str(ev)[:200])

    def close(self) -> None:
        self.closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class Brain:
    def __init__(self) -> None:
        self._session: _Session | None = None
        self._agent_id: str | None = None

    def available(self) -> bool:
        return bool(_env("ELEVENLABS_API_KEY"))

    def reset(self) -> None:
        """New chat: drop the session so ElevenLabs starts a fresh conversation."""
        if self._session:
            self._session.close()
        self._session = None

    def _get_session(self) -> _Session:
        if self._session is None or self._session.closed:
            if self._agent_id is None:
                self._agent_id = ensure_agent()
            self._session = _Session(self._agent_id)
        return self._session

    def stream(self, user_text: str, cancel: threading.Event) -> Iterator[str]:
        try:
            sess = self._get_session()
            yield from sess.ask(user_text, cancel)
        except BrainError:
            self.reset()
            raise
        except Exception as e:  # noqa: BLE001
            self.reset()
            raise BrainError(str(e) or e.__class__.__name__) from e
