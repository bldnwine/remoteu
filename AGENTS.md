# AGENTS.md — Developer & AI Agent Guide

## 1. Project Overview
`remoteu` is an ultra-low-latency, zero-dependency web-based remote trackpad & keyboard for Linux (Wayland / Hyprland & X11). It interfaces directly with kernel input devices via `/dev/uinput` using `python-evdev` and `aiohttp` native WebSockets.

- **Direct Hardware Input**: Replaces legacy tools (`dotool`, `xdotool`) with kernel `/dev/uinput` writes (<10µs latency).
- **Zero External CDNs**: Frontend is 100% self-contained in `templates/index.html` (fully functional offline).
- **Dual Framing Protocol**: High-frequency mouse/scroll streaming uses compact binary frames; control/text commands use JSON.

---

## 2. Tech Stack & Dependencies
- **Runtime**: Python >= 3.12, managed with `uv`.
- **Dependencies** (`pyproject.toml`):
  - `aiohttp>=3.9.0` (async HTTP server + native WebSocket)
  - `evdev>=1.7.0` (direct `/dev/uinput` virtual device)
  - `uvloop>=0.21` (Linux/CPython event loop; other platforms use stdlib asyncio)
- **Frontend**: Vanilla HTML5, CSS3, and JavaScript (`WebSocket`, `DataView`, `requestAnimationFrame`).

---

## 3. Architecture & File Layout

```
remoteu/
├── remoteu.py          # Backend server (aiohttp + UInput + event handlers + socket setup)
├── remoteu.service     # Systemd user service unit for autostart
├── setup.sh            # Automated installer (udev uaccess rules + sync + opt-in autostart)
├── setup.txt           # Quick reference setup guide
├── templates/
│   └── index.html      # Self-contained client UI (touch events + binary/JSON WebSocket)
├── tests/
│   ├── test_keymap.py   # Key mapping, ASCII, math symbols, modifiers, and combo tests
│   └── test_protocol.py # Binary protocol unpacking, boundary limits, and WS integration
├── pyproject.toml      # Project metadata and dependencies
└── uv.lock             # Exact dependency lockfile
```

---

## 4. Network & Framing Protocol

The WebSocket endpoint is served at `/ws`. The server socket sets `TCP_NODELAY = 1`, `TCP_QUICKACK = 1` (re-armed on packet reads), `heartbeat = None` (eliminating redundant 120Hz timer heap churn), and `compress = False`. All binary frames are unpacked with pre-compiled `struct.Struct` unpackers (`_UNPACK_MOUSE`, `_UNPACK_SCROLL`, `_UNPACK_PING`).

### A. Binary Frames (High-Frequency Streaming)
- **Mouse Delta (6 bytes)**:
  - Format: `!BhhB`
  - Byte 0: `0x01` (Type ID)
  - Bytes 1–2: `int16` $dx$ (big-endian)
  - Bytes 3–4: `int16` $dy$ (big-endian)
  - Byte 5: `uint8` flags (bit 0 = `drag`)
- **Scroll (3 bytes)**:
  - Format: `!Bh`
  - Byte 0: `0x02` (Type ID)
  - Bytes 1–2: `int16` amount (positive = up, negative = down)
- **Sequenced Mouse Delta (12 bytes)**:
  - Format: `!BHIhhB`
  - Byte 0: `0x04` (Type ID)
  - Bytes 1–2: `uint16` sequence number (wraps at 65536, shared with scroll)
  - Bytes 3–6: `uint32` client timestamp (ms modulo $2^{32}$)
  - Bytes 7–8: `int16` $dx$, Bytes 9–10: `int16` $dy$, Byte 11: `uint8` flags (bit 0 = `drag`)
- **Sequenced Scroll (9 bytes)**:
  - Format: `!BHIh`
  - Byte 0: `0x05` (Type ID), Bytes 1–2: `uint16` seq, Bytes 3–6: `uint32` client ts, Bytes 7–8: `int16` amount
- **Ping / Pong (5 bytes)**:
  - Format: `!BI`
  - Byte 0: `0x03` (Type ID)
  - Bytes 1–4: `uint32` client timestamp (milliseconds modulo $2^{32}$, big-endian; echoed back by server)

### B. JSON Text Frames (Control & Commands)
- **Type Text**: `{"type": "type_text", "text": "Hello World"}`
- **Key Combo**: `{"type": "key_combo", "modifiers": ["ctrl", "alt"], "key": "t"}`
- **Mouse Click**: `{"type": "mouse_click", "button": 1, "drag_end": false}` (buttons: 1=left, 2=middle, 3=right)
- **Motion Stats**: `{"type": "motion_stats"}` → server replies with frame counts, seq gaps, jitter, stalls, teleports
- **Motion Mode**: `{"type": "motion_mode", "mode": "smooth"|"direct"}` (default `smooth`; the client settings menu persists a toggle, `?motion=` overrides per page load)
- **System Command**: `{"type": "command", "name": "volume_up", "params": {}}`

---

## 5. Key Mapping & Injection Engine (`remoteu.py`)
- **`CHAR_MAP`**: Complete mapping for all 95 printable ASCII characters (codes 32–126), whitespace (`\t`, `\n`, `\r`, `\b`, ` `), mobile math symbols (`×`, `÷`, `−`, `±`), and smart typography (`“`, `”`, `‘`, `’`, `—`, `–`, `…`, `\u00a0`, `«`, `»`).
  - Returns `(keycode: int, needs_shift: bool)`.
- **Shift Synthesizer**: `handle_key_combo` and `handle_type_text` automatically synthesize `KEY_LEFTSHIFT` when typing shifted characters (e.g. `!`, `?`, uppercase letters, `+`).
- **`NAMED_KEYS` / `MOD_MAP`**: Canonical mappings for functional keys (`esc`, `tab`, `f11`, `space`, `return`, `backspace`, `delete`, arrow keys, etc.) and modifiers (`ctrl`, `alt`, `super`, `shift`).
- **Serialized Keyboard Injection**: A per-connection keyboard worker preserves message order while keeping the WebSocket receive loop responsive. Text bursts, key combinations, and discrete clicks are packed into one buffer with explicit `SYN_REPORT` boundaries and one `os.write()` call per action.
- **Background Command Worker**: A separate per-connection command queue runs subprocess commands without delaying trackpad, scroll, ping, or keyboard input. Commands remain ordered, and timed-out or cancelled subprocesses are killed and reaped.
- **Batched Kernel Event Injection**: `_UInputProxy.emit_rel_scroll` packs `REL_X`, `REL_Y`, legacy `REL_WHEEL`, `REL_WHEEL_HI_RES`, and `SYN_REPORT` into one native `@llHHi` buffer and injects it with one `os.write()` syscall.
- **Event Loop**: On Linux/CPython, the `uvloop` dependency is installed and activated before `asyncio.run()`; startup logs the selected event loop.

---

## 6. Client Touch & Jitter Control (`templates/index.html`)
- **Pointer Sampling**: Uses Pointer Events with `getCoalescedEvents()` when available, retaining a touch-event fallback for browsers without Pointer Events. The primary pointer ID is tracked explicitly for multi-touch gestures.
- **Hardware Touch Sampling**: Uses event timestamps with clamped $\Delta t \in [5\text{ms}, 50\text{ms}]$ to eliminate division-by-sub-millisecond velocity spikes when mobile browsers deliver queued input samples in batches.
- **Time-Decayed EMA Smoothing**: Libinput-style velocity tracking with $\alpha = 1 - e^{-\Delta t / 25.0}$ ensures mathematically consistent smoothing across 60Hz, 90Hz, and 120Hz displays.
- **Send Pacing & Anti-Collision**: Enforces `MIN_SEND_INTERVAL = 8ms` (~120Hz cap) and synchronizes `tpState.lastSendTime` across input sampling and `requestAnimationFrame(flushDelta)` to prevent clashing packet bursts.
- **True Adaptive Backpressure**: Checks `ws.bufferedAmount > 0` before transmitting. When 2.4 GHz Wi-Fi undergoes RF retries, delta motion is accumulated losslessly in `tpState.accDx`/`tpState.accDy` and transmitted as a single 6-byte packet once the link clears.
- **Server Micro-Burst Coalescing**: Server drains queued binary delta/scroll frames from the reader buffer, summing $\sum dx, \sum dy, \sum \text{scroll}$ and injecting them via batched single-syscall `emit_rel_scroll()`.
- **Motion Telemetry + Smoothing**: Sequenced frames feed per-connection stats (seq gaps, jitter EMA, stalls, teleports) queryable via `motion_stats`. Smooth mode is the default: adaptive jitter buffer (playout = 2×jitter, max 30 ms) with bounded dead reckoning (80 ms / 96 px caps, decayed velocity); `direct` is the zero-added-latency opt-out.
- **Live Latency Indicator**: `#status.status-line` acts as a dynamic pill measuring RTT via 5-byte ping every 1.5s (`🟢 <25ms`, `🟡 25-60ms`, `🔴 >60ms`).

---

## 7. Security & Permissions Architecture
- **No `input` Group Membership Required**: Adding users to the `input` group grants read access to physical `/dev/input/event*` devices, introducing keylogger vulnerabilities.
- **Seat ACLs (`TAG+="uaccess"`)**: `remoteu` uses `/etc/udev/rules.d/70-uinput.rules` with `TAG+="uaccess"`. `systemd-logind` assigns dynamic POSIX ACLs (`rw`) to the active seat user on `/dev/uinput` only.
- **Automated Setup**: Run `./setup.sh` (or `./setup.sh --autostart` / `--no-autostart`).

---

## 8. Development & Testing Commands

```bash
# Automated setup (udev rule + dependencies + optional autostart)
./setup.sh

# Sync dependencies
uv sync

# Run all unit and integration tests
uv run python -m unittest discover -s tests -p "test_*.py" -v

# Run backend server locally
uv run remoteu
# or: uv run python remoteu.py
```
