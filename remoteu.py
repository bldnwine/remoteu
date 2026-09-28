#!/usr/bin/env python3
"""
remoteu — phone remote for Linux.

Replaces dotool with direct /dev/uinput via python-evdev.
Async everything: aiohttp + python-socketio[asyncio].
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import string
import struct
import time
from typing import Any

from evdev import UInput, ecodes
from aiohttp import web, WSMessage

logger = logging.getLogger("remoteu")

# ── Pre-compiled binary protocol unpackers ──
_UNPACK_MOUSE = struct.Struct("!BhhB").unpack
_UNPACK_SCROLL = struct.Struct("!Bh").unpack
_UNPACK_PING = struct.Struct("!BI").unpack
# Sequenced frames add uint16 seq + uint32 client timestamp (ms mod 2**32).
_UNPACK_MOUSE_SEQ = struct.Struct("!BHIhhB").unpack
_UNPACK_SCROLL_SEQ = struct.Struct("!BHIh").unpack
_SEQ_MOD = 65536
_TS_MOD = 4294967296

# Native Linux struct input_event layout:
# timeval (tv_sec: long, tv_usec: long), type (u16), code (u16), value (s32)
# 24 bytes on 64-bit, 16 bytes on 32-bit
_INPUT_EVENT_FMT = "@llHHi"
_INPUT_EVENT_STRUCT = struct.Struct(_INPUT_EVENT_FMT)
_SYN_REPORT_BYTES = _INPUT_EVENT_STRUCT.pack(0, 0, ecodes.EV_SYN, ecodes.SYN_REPORT, 0)

# ── Build ecodes lookup from evdev KEY_ constants ──
_RAW = {
    k.removeprefix("KEY_"): v
    for k, v in vars(ecodes).items()
    if k.startswith("KEY_") and isinstance(v, int)
}

# Modifier-to-ecodes mapping
MOD_MAP: dict[str, int] = {
    "ctrl": ecodes.KEY_LEFTCTRL,
    "ctrl_l": ecodes.KEY_LEFTCTRL,
    "ctrl_r": ecodes.KEY_RIGHTCTRL,
    "control": ecodes.KEY_LEFTCTRL,
    "control_l": ecodes.KEY_LEFTCTRL,
    "control_r": ecodes.KEY_RIGHTCTRL,
    "shift": ecodes.KEY_LEFTSHIFT,
    "shift_l": ecodes.KEY_LEFTSHIFT,
    "shift_r": ecodes.KEY_RIGHTSHIFT,
    "alt": ecodes.KEY_LEFTALT,
    "alt_l": ecodes.KEY_LEFTALT,
    "alt_r": ecodes.KEY_RIGHTALT,
    "super": ecodes.KEY_LEFTMETA,
    "super_l": ecodes.KEY_LEFTMETA,
    "super_r": ecodes.KEY_RIGHTMETA,
    "meta": ecodes.KEY_LEFTMETA,
    "meta_l": ecodes.KEY_LEFTMETA,
    "meta_r": ecodes.KEY_RIGHTMETA,
    "win": ecodes.KEY_LEFTMETA,
    "cmd": ecodes.KEY_LEFTMETA,
}

# Direct ASCII character mapping -> (keycode, needs_shift)
CHAR_MAP: dict[str, tuple[int, bool]] = {
    # Whitespace / Control
    " ": (ecodes.KEY_SPACE, False),
    "\t": (ecodes.KEY_TAB, False),
    "\n": (ecodes.KEY_ENTER, False),
    "\r": (ecodes.KEY_ENTER, False),
    "\b": (ecodes.KEY_BACKSPACE, False),
    # Numbers
    "0": (ecodes.KEY_0, False),
    "1": (ecodes.KEY_1, False),
    "2": (ecodes.KEY_2, False),
    "3": (ecodes.KEY_3, False),
    "4": (ecodes.KEY_4, False),
    "5": (ecodes.KEY_5, False),
    "6": (ecodes.KEY_6, False),
    "7": (ecodes.KEY_7, False),
    "8": (ecodes.KEY_8, False),
    "9": (ecodes.KEY_9, False),
    # Shifted numbers (symbols)
    ")": (ecodes.KEY_0, True),
    "!": (ecodes.KEY_1, True),
    "@": (ecodes.KEY_2, True),
    "#": (ecodes.KEY_3, True),
    "$": (ecodes.KEY_4, True),
    "%": (ecodes.KEY_5, True),
    "^": (ecodes.KEY_6, True),
    "&": (ecodes.KEY_7, True),
    "*": (ecodes.KEY_8, True),
    "(": (ecodes.KEY_9, True),
    # Unshifted punctuation / symbols
    "-": (ecodes.KEY_MINUS, False),
    "=": (ecodes.KEY_EQUAL, False),
    "[": (ecodes.KEY_LEFTBRACE, False),
    "]": (ecodes.KEY_RIGHTBRACE, False),
    "\\": (ecodes.KEY_BACKSLASH, False),
    ";": (ecodes.KEY_SEMICOLON, False),
    "'": (ecodes.KEY_APOSTROPHE, False),
    "`": (ecodes.KEY_GRAVE, False),
    ",": (ecodes.KEY_COMMA, False),
    ".": (ecodes.KEY_DOT, False),
    "/": (ecodes.KEY_SLASH, False),
    # Shifted punctuation / symbols
    "_": (ecodes.KEY_MINUS, True),
    "+": (ecodes.KEY_EQUAL, True),
    "{": (ecodes.KEY_LEFTBRACE, True),
    "}": (ecodes.KEY_RIGHTBRACE, True),
    "|": (ecodes.KEY_BACKSLASH, True),
    ":": (ecodes.KEY_SEMICOLON, True),
    '"': (ecodes.KEY_APOSTROPHE, True),
    "~": (ecodes.KEY_GRAVE, True),
    "<": (ecodes.KEY_COMMA, True),
    ">": (ecodes.KEY_DOT, True),
    "?": (ecodes.KEY_SLASH, True),
    # Math symbols from mobile keyboards
    "×": (ecodes.KEY_8, True),  # U+00D7 multiplication -> *
    "÷": (ecodes.KEY_SLASH, False),  # U+00F7 division -> /
    "−": (ecodes.KEY_MINUS, False),  # U+2212 minus -> -
    "±": (ecodes.KEY_EQUAL, True),  # U+00B1 plus-minus -> +
    # Smart quotes & typography from mobile keyboards
    "‘": (ecodes.KEY_APOSTROPHE, False),  # U+2018 left single quote -> '
    "’": (ecodes.KEY_APOSTROPHE, False),  # U+2019 right single quote -> '
    "“": (ecodes.KEY_APOSTROPHE, True),  # U+201C left double quote -> "
    "”": (ecodes.KEY_APOSTROPHE, True),  # U+201D right double quote -> "
    "—": (ecodes.KEY_MINUS, False),  # U+2014 em dash -> -
    "–": (ecodes.KEY_MINUS, False),  # U+2013 en dash -> -
    "…": (ecodes.KEY_DOT, False),  # U+2026 ellipsis -> .
    "•": (ecodes.KEY_8, True),  # U+2022 bullet -> *
    "·": (ecodes.KEY_8, True),  # U+00B7 middle dot -> *
    "\u00a0": (ecodes.KEY_SPACE, False),  # Non-breaking space -> space
    "\u200b": (ecodes.KEY_SPACE, False),  # Zero-width space -> space
    "«": (ecodes.KEY_COMMA, True),  # U+00AB left guillemet -> <
    "»": (ecodes.KEY_DOT, True),  # U+00BB right guillemet -> >
    "‹": (ecodes.KEY_COMMA, True),  # U+2039 left single guillemet -> <
    "›": (ecodes.KEY_DOT, True),  # U+203A right single guillemet -> >
    "′": (ecodes.KEY_APOSTROPHE, False),  # Prime -> '
    "″": (ecodes.KEY_APOSTROPHE, True),  # Double prime -> "
}

# Add standard letters a-z and A-Z
for _c in string.ascii_lowercase:
    _code = getattr(ecodes, f"KEY_{_c.upper()}")
    CHAR_MAP[_c] = (_code, False)
    CHAR_MAP[_c.upper()] = (_code, True)

# Named keys map -> (keycode, needs_shift)
NAMED_KEYS: dict[str, tuple[int, bool]] = {
    "space": (ecodes.KEY_SPACE, False),
    "enter": (ecodes.KEY_ENTER, False),
    "return": (ecodes.KEY_ENTER, False),
    "escape": (ecodes.KEY_ESC, False),
    "esc": (ecodes.KEY_ESC, False),
    "backspace": (ecodes.KEY_BACKSPACE, False),
    "bksp": (ecodes.KEY_BACKSPACE, False),
    "tab": (ecodes.KEY_TAB, False),
    "delete": (ecodes.KEY_DELETE, False),
    "del": (ecodes.KEY_DELETE, False),
    "insert": (ecodes.KEY_INSERT, False),
    "ins": (ecodes.KEY_INSERT, False),
    "home": (ecodes.KEY_HOME, False),
    "end": (ecodes.KEY_END, False),
    "pageup": (ecodes.KEY_PAGEUP, False),
    "prior": (ecodes.KEY_PAGEUP, False),
    "pagedown": (ecodes.KEY_PAGEDOWN, False),
    "next": (ecodes.KEY_PAGEDOWN, False),
    "up": (ecodes.KEY_UP, False),
    "down": (ecodes.KEY_DOWN, False),
    "left": (ecodes.KEY_LEFT, False),
    "right": (ecodes.KEY_RIGHT, False),
    "capslock": (ecodes.KEY_CAPSLOCK, False),
    "numlock": (ecodes.KEY_NUMLOCK, False),
    "scrolllock": (ecodes.KEY_SCROLLLOCK, False),
    "printscreen": (ecodes.KEY_SYSRQ, False),
    "prtsc": (ecodes.KEY_SYSRQ, False),
    "print": (ecodes.KEY_SYSRQ, False),
    "pause": (ecodes.KEY_PAUSE, False),
    "break": (ecodes.KEY_PAUSE, False),
    "menu": (ecodes.KEY_COMPOSE, False),
    "compose": (ecodes.KEY_COMPOSE, False),
    # Math and currency aliases
    "multiply": (ecodes.KEY_8, True),
    "times": (ecodes.KEY_8, True),
    "divide": (ecodes.KEY_SLASH, False),
    "euro": (ecodes.KEY_EURO, False),
    "pound": (ecodes.KEY_3, True),
    "yen": (ecodes.KEY_YEN, False),
    "dollar": (ecodes.KEY_4, True),
    # Punctuation name aliases
    "minus": (ecodes.KEY_MINUS, False),
    "dash": (ecodes.KEY_MINUS, False),
    "underscore": (ecodes.KEY_MINUS, True),
    "equal": (ecodes.KEY_EQUAL, False),
    "plus": (ecodes.KEY_EQUAL, True),
    "leftbracket": (ecodes.KEY_LEFTBRACE, False),
    "bracketleft": (ecodes.KEY_LEFTBRACE, False),
    "leftbrace": (ecodes.KEY_LEFTBRACE, True),
    "braceleft": (ecodes.KEY_LEFTBRACE, True),
    "rightbracket": (ecodes.KEY_RIGHTBRACE, False),
    "bracketright": (ecodes.KEY_RIGHTBRACE, False),
    "rightbrace": (ecodes.KEY_RIGHTBRACE, True),
    "braceright": (ecodes.KEY_RIGHTBRACE, True),
    "semicolon": (ecodes.KEY_SEMICOLON, False),
    "colon": (ecodes.KEY_SEMICOLON, True),
    "apostrophe": (ecodes.KEY_APOSTROPHE, False),
    "quote": (ecodes.KEY_APOSTROPHE, False),
    "singlequote": (ecodes.KEY_APOSTROPHE, False),
    "doublequote": (ecodes.KEY_APOSTROPHE, True),
    "quotedbl": (ecodes.KEY_APOSTROPHE, True),
    "grave": (ecodes.KEY_GRAVE, False),
    "backtick": (ecodes.KEY_GRAVE, False),
    "tilde": (ecodes.KEY_GRAVE, True),
    "backslash": (ecodes.KEY_BACKSLASH, False),
    "bar": (ecodes.KEY_BACKSLASH, True),
    "pipe": (ecodes.KEY_BACKSLASH, True),
    "comma": (ecodes.KEY_COMMA, False),
    "less": (ecodes.KEY_COMMA, True),
    "period": (ecodes.KEY_DOT, False),
    "dot": (ecodes.KEY_DOT, False),
    "greater": (ecodes.KEY_DOT, True),
    "slash": (ecodes.KEY_SLASH, False),
    "question": (ecodes.KEY_SLASH, True),
    "exclamation": (ecodes.KEY_1, True),
    "exclam": (ecodes.KEY_1, True),
    "at": (ecodes.KEY_2, True),
    "hash": (ecodes.KEY_3, True),
    "numbersign": (ecodes.KEY_3, True),
    "percent": (ecodes.KEY_5, True),
    "caret": (ecodes.KEY_6, True),
    "asciicircum": (ecodes.KEY_6, True),
    "ampersand": (ecodes.KEY_7, True),
    "asterisk": (ecodes.KEY_8, True),
    "parenleft": (ecodes.KEY_9, True),
    "parenright": (ecodes.KEY_0, True),
}

MOUSE_BTN: dict[int, int] = {
    1: ecodes.BTN_LEFT,
    2: ecodes.BTN_MIDDLE,
    3: ecodes.BTN_RIGHT,
    4: ecodes.BTN_SIDE,
    5: ecodes.BTN_EXTRA,
}

# ── UInput device ──
# Declare the full set of capabilities we need.
_all_keys = {
    v
    for k, v in vars(ecodes).items()
    if (k.startswith("KEY_") or k.startswith("BTN_"))
    and isinstance(v, int)
    and v < ecodes.KEY_CNT
}
capabilities = {
    ecodes.EV_KEY: list(_all_keys),
    ecodes.EV_REL: [
        ecodes.REL_X,
        ecodes.REL_Y,
        ecodes.REL_WHEEL,
        ecodes.REL_WHEEL_HI_RES,
        ecodes.REL_HWHEEL,
        ecodes.REL_HWHEEL_HI_RES,
    ],
}


class _UInputProxy:
    """Lazy initialization proxy for evdev UInput virtual device with batched write support."""

    def __init__(self):
        self._dev: UInput | None = None

    @property
    def dev(self) -> UInput:
        if self._dev is None:
            self._dev = UInput(name="remoteu-virtual-input", phys="remoteu", events=capabilities)
            logger.info("UInput device created (virtual input)")
        return self._dev

    @property
    def fd(self) -> int:
        return self.dev.fd

    def write(self, ev_type: int, code: int, value: int):
        return self.dev.write(ev_type, code, value)

    def syn(self):
        return self.dev.syn()

    def _using_mock_hooks(self) -> bool:
        return (
            getattr(self.write, "__func__", self.write) is not _UInputProxy.write
            or getattr(self.syn, "__func__", self.syn) is not _UInputProxy.syn
        )

    def emit_rel_scroll(self, dx: int, dy: int, scroll: int = 0):
        """Emit motion and wheel events in one buffered SYN_REPORT frame."""
        events = []
        if dx != 0:
            events.append((ecodes.EV_REL, ecodes.REL_X, dx))
        if dy != 0:
            events.append((ecodes.EV_REL, ecodes.REL_Y, dy))
        if scroll != 0:
            events.append((ecodes.EV_REL, ecodes.REL_WHEEL, scroll))
            events.append((ecodes.EV_REL, ecodes.REL_WHEEL_HI_RES, scroll * 120))
        if not events:
            return

        # If test suite or caller has mocked write or syn, fall back for test compatibility.
        if self._using_mock_hooks():
            for ev_type, code, value in events:
                self.write(ev_type, code, value)
            self.syn()
            return

        buf = bytearray()
        for ev_type, code, value in events:
            buf.extend(_INPUT_EVENT_STRUCT.pack(0, 0, ev_type, code, value))
        buf.extend(_SYN_REPORT_BYTES)
        os.write(self.fd, buf)

    def emit_rel_xy(self, dx: int, dy: int):
        """Emit REL_X + REL_Y + SYN_REPORT in one write() syscall."""
        self.emit_rel_scroll(dx, dy)

    def emit_scroll(self, amount: int):
        """Emit legacy and high-resolution wheel events in one write() syscall."""
        self.emit_rel_scroll(0, 0, amount)

    def close(self):
        if self._dev is not None:
            self._dev.close()
            self._dev = None


ui = _UInputProxy()

# ── Web Application Template Directory ──
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")


async def index(request: web.Request) -> web.Response:
    html_path = os.path.join(TEMPLATE_DIR, "index.html")
    with open(html_path) as f:
        return web.Response(text=f.read(), content_type="text/html")


# ── Helpers ──

async def _terminate_process(proc: asyncio.subprocess.Process):
    """Terminate and reap a subprocess after timeout or cancellation."""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await proc.wait()


async def run_cmd(*args: str) -> tuple[bool, str]:
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode == 0:
            return True, stdout.decode().strip()
        return False, stderr.decode().strip()
    except FileNotFoundError:
        return False, f"{args[0]} not found"
    except asyncio.TimeoutError:
        if proc is not None:
            await _terminate_process(proc)
        return False, f"{args[0]} timed out"
    except asyncio.CancelledError:
        if proc is not None:
            await _terminate_process(proc)
        raise
    except Exception as e:
        if proc is not None and proc.returncode is None:
            await _terminate_process(proc)
        return False, str(e)


def _append_key_event(events: list[tuple[int, int, int]], code: int, value: int):
    events.append((ecodes.EV_KEY, code, value))


def _emit_key_events(events: list[tuple[int, int, int]]):
    """Write a key sequence with one report per transition."""
    if not events:
        return

    if ui._using_mock_hooks():
        for ev_type, code, value in events:
            ui.write(ev_type, code, value)
            ui.syn()
        return

    buf = bytearray()
    for ev_type, code, value in events:
        buf.extend(_INPUT_EVENT_STRUCT.pack(0, 0, ev_type, code, value))
        buf.extend(_SYN_REPORT_BYTES)
    os.write(ui.fd, buf)


def key_down(code: int):
    """Press and report a persistent key/button state."""
    ui.write(ecodes.EV_KEY, code, 1)
    ui.syn()


def key_up(code: int):
    """Release and report a persistent key/button state."""
    ui.write(ecodes.EV_KEY, code, 0)
    ui.syn()


def emit_click(code: int):
    """Emit a zero-hold click in one write, with explicit down/up reports."""
    events = []
    _append_key_event(events, code, 1)
    _append_key_event(events, code, 0)
    _emit_key_events(events)


def resolve_key(name: str) -> tuple[int | None, bool]:
    """
    Resolve a key name or character to (keycode, needs_shift).
    Returns (None, False) if the key cannot be resolved.
    """
    if not name:
        return None, False
    # Exact character match (case sensitive e.g. 'a' vs 'A' vs '!')
    if name in CHAR_MAP:
        return CHAR_MAP[name]
    low = name.lower()
    if low in MOD_MAP:
        return MOD_MAP[low], False
    if low in NAMED_KEYS:
        return NAMED_KEYS[low]
    # Fallback to direct ecodes KEY_* constant (e.g. 'f11', 'mute', etc.)
    clean = name.upper().removeprefix("KEY_")
    if clean in _RAW:
        return _RAW[clean], False
    return None, False


# ── Input & Command Handlers ──

def handle_key_combo(data: dict[str, Any]):
    modifiers = data.get("modifiers", [])
    key = data.get("key", "")
    if not key:
        return

    key_code, key_needs_shift = resolve_key(key)
    if key_code is None:
        logger.warning("Key combo failed: unknown key %r", key)
        return

    mod_codes = []
    has_shift = False
    for m in modifiers:
        mod_code, _ = resolve_key(m)
        if mod_code is not None:
            mod_codes.append(mod_code)
            if mod_code in (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT):
                has_shift = True

    # If the key requires shift (e.g. '+', ':', '?') and shift wasn't explicitly in modifiers,
    # add KEY_LEFTSHIFT to produce the expected symbol.
    if key_needs_shift and not has_shift:
        mod_codes.append(ecodes.KEY_LEFTSHIFT)

    events = []
    for code in mod_codes:
        _append_key_event(events, code, 1)
    _append_key_event(events, key_code, 1)
    _append_key_event(events, key_code, 0)
    for code in reversed(mod_codes):
        _append_key_event(events, code, 0)
    _emit_key_events(events)


def handle_type_text(data: dict[str, Any]):
    text = data.get("text", "")
    events = []
    for char in text:
        key_code, needs_shift = resolve_key(char)
        if key_code is None:
            logger.warning("Cannot type unmapped character: %r", char)
            continue
        if needs_shift:
            _append_key_event(events, ecodes.KEY_LEFTSHIFT, 1)
        _append_key_event(events, key_code, 1)
        _append_key_event(events, key_code, 0)
        if needs_shift:
            _append_key_event(events, ecodes.KEY_LEFTSHIFT, 0)
    _emit_key_events(events)


async def handle_command(ws: web.WebSocketResponse, data: dict[str, Any]):
    command = data.get("name", "")
    params = data.get("params", {})

    if command == "volume_up":
        success, msg = await run_cmd("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.05+")
    elif command == "volume_down":
        success, msg = await run_cmd("wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.05-")
    elif command == "volume_mute":
        success, msg = await run_cmd("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle")
    elif command in ("media_play", "media_pause", "media_play_pause", "media_next",
                     "media_previous", "media_stop"):
        pc_actions = {
            "media_play": "play", "media_pause": "pause",
            "media_play_pause": "play-pause", "media_next": "next",
            "media_previous": "previous", "media_stop": "stop",
        }
        success, msg = await run_cmd("playerctl", pc_actions[command])
    elif command == "shutdown":
        success, msg = await run_cmd("shutdown", "now")
    elif command == "reboot":
        success, msg = await run_cmd("reboot")
    elif command == "suspend":
        success, msg = await run_cmd("systemctl", "suspend")
    elif command == "hibernate":
        success, msg = await run_cmd("systemctl", "hibernate")
    elif command == "dpms_on":
        success, msg = await run_cmd("hyprctl", "dispatch", "dpms", "on")
    elif command == "dpms_off":
        success, msg = await run_cmd("hyprctl", "dispatch", "dpms", "off")
    elif command == "cursor_absolute":
        x = str(params.get("x", 0))
        y = str(params.get("y", 0))
        success, msg = await run_cmd("hyprctl", "dispatch", "movecursor", x, y)
    else:
        await ws.send_json({"type": "error", "message": f"Unknown command: {command}"})
        return

    if not success:
        await ws.send_json({"type": "error", "message": msg})


async def _send_ws_error(ws: web.WebSocketResponse, message: str):
    try:
        await ws.send_json({"type": "error", "message": message})
    except Exception:
        pass


async def _keyboard_worker(
    ws: web.WebSocketResponse,
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
):
    """Serialize discrete keyboard and mouse-button input for one connection."""
    while True:
        msg_type, data = await queue.get()
        try:
            if msg_type == "type_text":
                handle_type_text(data)
            elif msg_type == "key_combo":
                handle_key_combo(data)
            elif msg_type == "mouse_click":
                button = int(data.get("button", 1))
                code = MOUSE_BTN.get(button, ecodes.BTN_LEFT)
                emit_click(code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Error handling queued keyboard input: %s", e)
            await _send_ws_error(ws, f"Input error: {e}")
        finally:
            queue.task_done()


async def _command_worker(
    ws: web.WebSocketResponse,
    queue: asyncio.Queue[dict[str, Any]],
):
    """Run commands independently of, but in order with, connection input."""
    while True:
        data = await queue.get()
        try:
            await handle_command(ws, data)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Error handling queued command: %s", e)
            await _send_ws_error(ws, f"Command error: {e}")
        finally:
            queue.task_done()


# ── WebSocket Route Handler ──

STALL_GAP_S = 0.05  # arrival gap counted as a stall while a gesture is active
TELEPORT_PX = 200  # coalesced burst magnitude counted as a teleport


class MotionStats:
    """Per-connection motion telemetry: seq gaps, jitter, stalls, teleports."""

    def __init__(self, time_fn=None):
        self._now = time_fn or time.monotonic
        self.frames = 0
        self.sequenced_frames = 0
        self.seq_gaps = 0
        self.jitter_ms = 0.0
        self.stalls = 0
        self.teleports = 0
        self.max_burst_px = 0
        self._last_seq: int | None = None
        self._last_client_ts: int | None = None
        self._last_arrival: float | None = None

    def _seq_forward_distance(self, seq: int) -> int | None:
        if self._last_seq is None:
            return None
        return (seq - self._last_seq) % _SEQ_MOD

    def record(self, seq: int | None, client_ts: int | None, dx: int, dy: int):
        now = self._now()
        self.frames += 1
        burst = abs(dx) + abs(dy)
        if burst > self.max_burst_px:
            self.max_burst_px = burst
        if burst >= TELEPORT_PX:
            self.teleports += 1
        if self._last_arrival is not None and (now - self._last_arrival) >= STALL_GAP_S:
            self.stalls += 1
        if seq is not None:
            self.sequenced_frames += 1
            dist = self._seq_forward_distance(seq)
            if dist is not None and dist > 1:
                self.seq_gaps += dist - 1
            self._last_seq = seq
        if client_ts is not None and self._last_client_ts is not None and self._last_arrival is not None:
            client_delta = (client_ts - self._last_client_ts) % _TS_MOD
            arrival_delta_ms = (now - self._last_arrival) * 1000.0
            self.jitter_ms += (abs(arrival_delta_ms - client_delta) - self.jitter_ms) / 16.0
        if client_ts is not None:
            self._last_client_ts = client_ts
        self._last_arrival = now

    def snapshot(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "sequenced_frames": self.sequenced_frames,
            "seq_gaps": self.seq_gaps,
            "jitter_ms": round(self.jitter_ms, 2),
            "stalls": self.stalls,
            "teleports": self.teleports,
            "max_burst_px": self.max_burst_px,
        }


class MotionSmoother:
    """Adaptive jitter buffer with bounded dead-reckoning for pointer motion.

    Smooth mode is the default: frames are held for an adaptive playout delay
    (2x measured jitter, max 30ms, collapsing to ~0 on clean links) and brief
    decayed extrapolation covers stalls. Direct mode is a pass-through with
    zero added latency. Scroll/click/key paths are untouched.
    """

    def __init__(self, emit=None, time_fn=None, flush_interval: float = 0.008, enabled: bool = True, on_error=None):
        self._emit = emit or (lambda dx, dy, scroll: ui.emit_rel_scroll(dx, dy, scroll))
        self._now = time_fn or time.monotonic
        self.flush_interval = flush_interval
        self.enabled = enabled
        self._on_error = on_error
        self.predict_enabled = True
        self._queue: list[tuple[float, int, int, int]] = []  # (arrival, dx, dy, scroll)
        self._vel_x = 0.0
        self._vel_y = 0.0
        self._last_emit: float | None = None
        self._last_arrival: float | None = None
        self._predicted_px = 0.0
        self._predicted_s = 0.0
        self._jitter_s = 0.0

    @property
    def playout_delay(self) -> float:
        return 0.0 if not self.enabled else min(0.030, max(0.0, 2.0 * self._jitter_s))

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if not enabled:
            self.flush()
        else:
            self._predicted_px = 0.0
            self._predicted_s = 0.0

    def set_jitter(self, jitter_ms: float):
        self._jitter_s = max(0.0, jitter_ms) / 1000.0

    def push(self, dx: int, dy: int, scroll: int = 0):
        now = self._now()
        self._last_arrival = now
        self._predicted_px = 0.0
        self._predicted_s = 0.0
        if not self.enabled:
            self._emit_direct(dx, dy, scroll, now)
            return
        self._queue.append((now, dx, dy, scroll))

    def _emit_direct(self, dx: int, dy: int, scroll: int, now: float, dt_hint: float | None = None):
        if dx == 0 and dy == 0 and scroll == 0:
            return
        self._emit(dx, dy, scroll)
        if dt_hint is not None:
            alpha = 0.3
            self._vel_x += alpha * (dx / max(1e-3, dt_hint) - self._vel_x)
            self._vel_y += alpha * (dy / max(1e-3, dt_hint) - self._vel_y)
        else:
            self._track_velocity(dx, dy, now)

    def _track_velocity(self, dx: int, dy: int, now: float):
        if self._last_emit is not None:
            dt = max(1e-3, now - self._last_emit)
            alpha = 0.3
            self._vel_x += alpha * (dx / dt - self._vel_x)
            self._vel_y += alpha * (dy / dt - self._vel_y)
        self._last_emit = now

    def flush(self):
        if not self._queue:
            return
        total_dx = sum(f[1] for f in self._queue)
        total_dy = sum(f[2] for f in self._queue)
        total_scroll = sum(f[3] for f in self._queue)
        self._queue.clear()
        self._emit_direct(total_dx, total_dy, total_scroll, self._now())

    def tick(self):
        now = self._now()
        if not self.enabled:
            return
        delay = self.playout_delay
        due_dx = due_dy = due_scroll = 0
        oldest: float | None = None
        remaining = []
        for arrival, dx, dy, scroll in self._queue:
            if arrival + delay <= now:
                due_dx += dx
                due_dy += dy
                due_scroll += scroll
                if oldest is None or arrival < oldest:
                    oldest = arrival
            else:
                remaining.append((arrival, dx, dy, scroll))
        self._queue = remaining
        if due_dx or due_dy or due_scroll:
            span = max(0.004, now - oldest) if oldest is not None else None
            self._emit_direct(due_dx, due_dy, due_scroll, now, dt_hint=span)
            return
        # Dead reckoning: brief decayed extrapolation during stalls.
        if (
            self.predict_enabled
            and not self._queue
            and self._last_arrival is not None
            and now - self._last_arrival >= 0.025
            and self._predicted_s < 0.080
            and self._predicted_px < 96.0
        ):
            speed = (self._vel_x**2 + self._vel_y**2) ** 0.5
            if speed > 500.0:  # px/s
                step = 0.008
                step_dx = int(round(self._vel_x * step))
                step_dy = int(round(self._vel_y * step))
                if step_dx or step_dy:
                    self._emit(step_dx, step_dy, 0)
                    self._last_emit = now
                    self._predicted_px += abs(step_dx) + abs(step_dy)
                    self._predicted_s += step
                    self._vel_x *= 0.9
                    self._vel_y *= 0.9

    async def run(self):
        while True:
            await asyncio.sleep(self.flush_interval)
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("Motion smoother tick failed: %s", e)
                if self._on_error is not None:
                    try:
                        await self._on_error(e)
                    except Exception:
                        pass


def _drain_queued_binary_frames(ws: web.WebSocketResponse) -> list[WSMessage]:
    """Drain any pending binary messages already in the asyncio reader queue."""
    batch: list[WSMessage] = []
    reader = getattr(ws, "_reader", None)
    buffer = getattr(reader, "_buffer", None)
    if buffer:
        try:
            while buffer:
                item = buffer[0]
                msg = item[0] if isinstance(item, tuple) else item
                if getattr(msg, "type", None) == web.WSMsgType.BINARY:
                    buffer.popleft()
                    batch.append(msg)
                else:
                    break
        except Exception as e:
            logger.debug("Error draining reader queue: %s", e)
    return batch


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=None, compress=False)
    await ws.prepare(request)
    logger.info("WebSocket client connected")

    # Configure low-latency TCP socket options (disable Nagle, enable QuickACK)
    client_sock = None
    try:
        transport = request.transport
        if transport is not None:
            client_sock = transport.get_extra_info("socket")
            if client_sock is not None:
                if hasattr(socket, "TCP_NODELAY"):
                    client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                if hasattr(socket, "TCP_QUICKACK"):
                    client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
    except Exception as e:
        logger.debug("Could not set TCP socket options: %s", e)

    def _rearm_quickack():
        if client_sock is not None and hasattr(socket, "TCP_QUICKACK"):
            try:
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
            except Exception:
                pass

    is_dragging = False
    motion_stats = MotionStats()

    async def _motion_error(e: Exception):
        await _send_ws_error(ws, f"Input error: {e}")

    smoother = MotionSmoother(on_error=_motion_error)
    keyboard_queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
    command_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    background_workers = (
        asyncio.create_task(
            _keyboard_worker(ws, keyboard_queue), name="remoteu-keyboard-worker"
        ),
        asyncio.create_task(
            _command_worker(ws, command_queue), name="remoteu-command-worker"
        ),
        asyncio.create_task(smoother.run(), name="remoteu-motion-smoother"),
    )

    def _motion_frames(first: bytes) -> tuple[
        list[tuple[int, int, int, bool, int | None, int | None]], list[bytes]
    ]:
        """Parse the current plus drained binary frames into motion tuples.

        Returns ([(dx, dy, scroll, drag, seq, client_ts)], [ping frames to echo]).
        Unknown frames are ignored.
        """
        out: list[tuple[int, int, int, bool, int | None, int | None]] = []
        pongs: list[bytes] = []

        def _parse(data: bytes):
            if len(data) == 12 and data[0] == 4:
                _, seq, cts, dx, dy, flags = _UNPACK_MOUSE_SEQ(data)
                return (dx, dy, 0, bool(flags & 1), seq, cts)
            if len(data) == 9 and data[0] == 5:
                _, seq, cts, amount = _UNPACK_SCROLL_SEQ(data)
                return (0, 0, amount, False, seq, cts)
            if len(data) == 6 and data[0] == 1:
                _, dx, dy, flags = _UNPACK_MOUSE(data)
                return (dx, dy, 0, bool(flags & 1), None, None)
            if len(data) == 3 and data[0] == 2:
                _, amount = _UNPACK_SCROLL(data)
                return (0, 0, amount, False, None, None)
            return None

        parsed = _parse(first)
        if parsed is not None:
            out.append(parsed)
        for extra_msg in _drain_queued_binary_frames(ws):
            extra = extra_msg.data
            if len(extra) == 5 and extra[0] == 3:
                pongs.append(extra)
                continue
            parsed = _parse(extra)
            if parsed is not None:
                out.append(parsed)
        return out, pongs

    try:
        await ws.send_json({"type": "connected", "status": "ready"})
        async for msg in ws:
            try:
                if msg.type == web.WSMsgType.BINARY:
                    _rearm_quickack()
                    data = msg.data
                    # 0x01/0x04: Mouse delta frames (legacy 6B or sequenced 12B)
                    # 0x02/0x05: Scroll frames (legacy 3B or sequenced 9B)
                    if data[:1] in (b"\x01", b"\x02", b"\x04", b"\x05") and len(data) in (3, 6, 9, 12):
                        frames, pongs = _motion_frames(data)
                        for pong in pongs:
                            await ws.send_bytes(pong)
                        # Drag button transitions stay immediate; payloads may be smoothed.
                        for dx, dy, scroll, drag, seq, cts in frames:
                            if drag and not is_dragging:
                                is_dragging = True
                                key_down(MOUSE_BTN[1])
                            motion_stats.record(seq, cts, dx, dy)
                        smoother.set_jitter(motion_stats.jitter_ms)
                        if smoother.enabled:
                            for dx, dy, scroll, drag, seq, cts in frames:
                                smoother.push(dx, dy, scroll)
                        else:
                            total_dx = sum(f[0] for f in frames)
                            total_dy = sum(f[1] for f in frames)
                            total_scroll = sum(f[2] for f in frames)
                            ui.emit_rel_scroll(total_dx, total_dy, total_scroll)

                    # 0x03: Ping/Pong frame (5 bytes: !BI)
                    elif len(data) == 5 and data[0] == 3:
                        await ws.send_bytes(data)

                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception as e:
                        logger.warning("Invalid JSON received: %s", e)
                        continue

                    msg_type = data.get("type", "")

                    if msg_type == "mouse_delta":
                        drag = data.get("drag", False)
                        if drag and not is_dragging:
                            is_dragging = True
                            key_down(MOUSE_BTN[1])

                        dx = int(data.get("dx", 0))
                        dy = int(data.get("dy", 0))
                        if dx != 0:
                            ui.write(ecodes.EV_REL, ecodes.REL_X, dx)
                        if dy != 0:
                            ui.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                        if dx != 0 or dy != 0:
                            ui.syn()

                    elif msg_type == "mouse_click":
                        if data.get("drag_end"):
                            if is_dragging:
                                is_dragging = False
                                key_up(MOUSE_BTN[1])
                            continue
                        keyboard_queue.put_nowait(("mouse_click", data))

                    elif msg_type == "mouse_scroll":
                        direction = data.get("direction", "up")
                        amount = int(data.get("amount", 1))
                        value = amount if direction == "up" else -amount
                        ui.emit_scroll(value)

                    elif msg_type == "key_combo":
                        keyboard_queue.put_nowait((msg_type, data))

                    elif msg_type == "type_text":
                        keyboard_queue.put_nowait((msg_type, data))

                    elif msg_type == "motion_stats":
                        try:
                            await ws.send_json({"type": "motion_stats", "stats": motion_stats.snapshot()})
                        except Exception:
                            pass

                    elif msg_type == "motion_mode":
                        mode = str(data.get("mode", "direct")).lower()
                        smoother.set_enabled(mode == "smooth")
                        try:
                            await ws.send_json({"type": "motion_mode", "mode": "smooth" if smoother.enabled else "direct"})
                        except Exception:
                            pass

                    elif msg_type == "command":
                        command_queue.put_nowait(data)

                elif msg.type == web.WSMsgType.ERROR:
                    logger.warning("WebSocket error: %s", ws.exception())

            except Exception as e:
                logger.exception("Error handling input message: %s", e)
                try:
                    await ws.send_json({"type": "error", "message": f"Input error: {e}"})
                except Exception:
                    pass

    finally:
        smoother.flush()
        for worker in background_workers:
            worker.cancel()
        await asyncio.gather(*background_workers, return_exceptions=True)
        if is_dragging:
            try:
                key_up(MOUSE_BTN[1])
            except Exception:
                pass
        logger.info("WebSocket client disconnected (motion: %s)", motion_stats.snapshot())

    return ws


def create_app() -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    return app


# ── Startup check ──


def main():
    logging.basicConfig(level=logging.INFO)
    print("=" * 60)
    print("remoteu starting...")
    print("=" * 60)
    print("Access from phone: http://YOUR_IP:5000")
    print("-" * 60)

    if not os.access("/dev/uinput", os.W_OK):
        print("\n[!] WARNING: /dev/uinput is not writable by current user!")
        print("    Do NOT add yourself to the 'input' group (security risk).")
        print("    To grant access securely via udev uaccess:")
        print("      ./setup.sh")
        print("    Or manually:")
        print('      echo \'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"\' | sudo tee /etc/udev/rules.d/70-uinput.rules')
        print("      sudo udevadm control --reload-rules && sudo udevadm trigger /dev/uinput\n")
    else:
        print("ok /dev/uinput writable")

    if shutil.which("dotool"):
        print("note: dotool is installed but unused (remoteu uses evdev directly)")

    print("starting server on http://0.0.0.0:5000")

    try:
        import uvloop
        uvloop.install()
        logger.info("Using uvloop event loop")
    except ImportError:
        logger.info("Using standard asyncio event loop")

    asyncio.run(_async_main())


async def _async_main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.bind(("0.0.0.0", 5000))
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        ui.close()


if __name__ == "__main__":
    main()