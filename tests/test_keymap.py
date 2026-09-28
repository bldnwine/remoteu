import asyncio
import unittest
import string
from evdev import ecodes
import remoteu


class TestKeymap(unittest.IsolatedAsyncioTestCase):
    def test_all_printable_ascii_characters(self):
        """Ensure all 95 printable ASCII characters (codes 32 to 126) resolve cleanly."""
        for code_point in range(32, 127):
            char = chr(code_point)
            key_code, needs_shift = remoteu.resolve_key(char)
            self.assertIsNotNone(
                key_code, f"Character {char!r} (ASCII {code_point}) failed to resolve"
            )
            self.assertIsInstance(key_code, int)
            self.assertIsInstance(needs_shift, bool)

    def test_space_resolution(self):
        """Space character and named 'space' must resolve to KEY_SPACE."""
        code1, shift1 = remoteu.resolve_key(" ")
        code2, shift2 = remoteu.resolve_key("space")
        self.assertEqual(code1, ecodes.KEY_SPACE)
        self.assertFalse(shift1)
        self.assertEqual(code2, ecodes.KEY_SPACE)
        self.assertFalse(shift2)

    def test_letters_case(self):
        """Lowercase letters should have needs_shift=False, uppercase needs_shift=True."""
        for c in string.ascii_lowercase:
            code_l, shift_l = remoteu.resolve_key(c)
            code_u, shift_u = remoteu.resolve_key(c.upper())
            self.assertEqual(code_l, code_u)
            self.assertFalse(shift_l)
            self.assertTrue(shift_u)

    def test_digits_and_shifted_symbols(self):
        """Check number keys and shifted number symbols."""
        expected_shifted = {
            ")": ecodes.KEY_0,
            "!": ecodes.KEY_1,
            "@": ecodes.KEY_2,
            "#": ecodes.KEY_3,
            "$": ecodes.KEY_4,
            "%": ecodes.KEY_5,
            "^": ecodes.KEY_6,
            "&": ecodes.KEY_7,
            "*": ecodes.KEY_8,
            "(": ecodes.KEY_9,
        }
        for sym, expected_code in expected_shifted.items():
            code, shift = remoteu.resolve_key(sym)
            self.assertEqual(code, expected_code, f"Failed for symbol {sym}")
            self.assertTrue(shift, f"Shift should be True for {sym}")

    def test_punctuation_and_shifted_punctuation(self):
        """Check unshifted and shifted punctuation characters."""
        unshifted = {
            "-": ecodes.KEY_MINUS,
            "=": ecodes.KEY_EQUAL,
            "[": ecodes.KEY_LEFTBRACE,
            "]": ecodes.KEY_RIGHTBRACE,
            "\\": ecodes.KEY_BACKSLASH,
            ";": ecodes.KEY_SEMICOLON,
            "'": ecodes.KEY_APOSTROPHE,
            "`": ecodes.KEY_GRAVE,
            ",": ecodes.KEY_COMMA,
            ".": ecodes.KEY_DOT,
            "/": ecodes.KEY_SLASH,
        }
        for ch, expected_code in unshifted.items():
            code, shift = remoteu.resolve_key(ch)
            self.assertEqual(code, expected_code, f"Failed for unshifted {ch}")
            self.assertFalse(shift, f"Shift should be False for {ch}")

        shifted = {
            "_": ecodes.KEY_MINUS,
            "+": ecodes.KEY_EQUAL,
            "{": ecodes.KEY_LEFTBRACE,
            "}": ecodes.KEY_RIGHTBRACE,
            "|": ecodes.KEY_BACKSLASH,
            ":": ecodes.KEY_SEMICOLON,
            '"': ecodes.KEY_APOSTROPHE,
            "~": ecodes.KEY_GRAVE,
            "<": ecodes.KEY_COMMA,
            ">": ecodes.KEY_DOT,
            "?": ecodes.KEY_SLASH,
        }
        for ch, expected_code in shifted.items():
            code, shift = remoteu.resolve_key(ch)
            self.assertEqual(code, expected_code, f"Failed for shifted {ch}")
            self.assertTrue(shift, f"Shift should be True for {ch}")

    def test_named_keys(self):
        """Check named keys from frontend UI."""
        named = {
            "enter": ecodes.KEY_ENTER,
            "return": ecodes.KEY_ENTER,
            "escape": ecodes.KEY_ESC,
            "esc": ecodes.KEY_ESC,
            "tab": ecodes.KEY_TAB,
            "backspace": ecodes.KEY_BACKSPACE,
            "delete": ecodes.KEY_DELETE,
            "del": ecodes.KEY_DELETE,
            "f11": ecodes.KEY_F11,
            "left": ecodes.KEY_LEFT,
            "right": ecodes.KEY_RIGHT,
            "up": ecodes.KEY_UP,
            "down": ecodes.KEY_DOWN,
        }
        for name, expected_code in named.items():
            code, _ = remoteu.resolve_key(name)
            self.assertEqual(code, expected_code, f"Failed for named key {name}")

    def test_modifiers(self):
        """Check modifier keys."""
        mods = {
            "ctrl": ecodes.KEY_LEFTCTRL,
            "shift": ecodes.KEY_LEFTSHIFT,
            "alt": ecodes.KEY_LEFTALT,
            "super": ecodes.KEY_LEFTMETA,
            "meta": ecodes.KEY_LEFTMETA,
            "win": ecodes.KEY_LEFTMETA,
        }
        for mod, expected_code in mods.items():
            code, _ = remoteu.resolve_key(mod)
            self.assertEqual(code, expected_code, f"Failed for modifier {mod}")

    async def test_async_type_text(self):
        """Test text keys are emitted in a complete down/up sequence."""
        events = []

        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = lambda ev_type, code, value: events.append(
                ("down" if value else "up", code)
            )
            remoteu.ui.syn = lambda: None

            # Repeated keys must not overlap, and Shift must remain held
            # until after the shifted character has been released.
            remoteu.handle_type_text({"text": "Aaa"})

            expected_events = [
                ("down", ecodes.KEY_LEFTSHIFT),
                ("down", ecodes.KEY_A),
                ("up", ecodes.KEY_A),
                ("up", ecodes.KEY_LEFTSHIFT),
                ("down", ecodes.KEY_A),
                ("up", ecodes.KEY_A),
                ("down", ecodes.KEY_A),
                ("up", ecodes.KEY_A),
            ]
            self.assertEqual(events, expected_events)
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn

    async def test_async_key_combo(self):
        """Test key combos keep modifiers held until the main key is released."""
        events = []
        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = lambda ev_type, code, value: events.append(
                ("down" if value else "up", code)
            )
            remoteu.ui.syn = lambda: None

            # Ctrl + '+' -> press Ctrl + Shift, tap EQUAL, release EQUAL + Shift + Ctrl
            remoteu.handle_key_combo({"modifiers": ["ctrl"], "key": "+"})

            expected = [
                ("down", ecodes.KEY_LEFTCTRL),
                ("down", ecodes.KEY_LEFTSHIFT),
                ("down", ecodes.KEY_EQUAL),
                ("up", ecodes.KEY_EQUAL),
                ("up", ecodes.KEY_LEFTSHIFT),
                ("up", ecodes.KEY_LEFTCTRL),
            ]
            self.assertEqual(events, expected)

            events.clear()
            # Super + ' '
            remoteu.handle_key_combo({"modifiers": ["super"], "key": " "})
            expected_super_space = [
                ("down", ecodes.KEY_LEFTMETA),
                ("down", ecodes.KEY_SPACE),
                ("up", ecodes.KEY_SPACE),
                ("up", ecodes.KEY_LEFTMETA),
            ]
            self.assertEqual(events, expected_super_space)
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn

    def test_mobile_math_and_smart_punctuation(self):
        """Check mobile math symbols and smart typography mapping."""
        expected = {
            "×": (ecodes.KEY_8, True),
            "÷": (ecodes.KEY_SLASH, False),
            "−": (ecodes.KEY_MINUS, False),
            "±": (ecodes.KEY_EQUAL, True),
            "“": (ecodes.KEY_APOSTROPHE, True),
            "”": (ecodes.KEY_APOSTROPHE, True),
            "‘": (ecodes.KEY_APOSTROPHE, False),
            "’": (ecodes.KEY_APOSTROPHE, False),
            "—": (ecodes.KEY_MINUS, False),
            "–": (ecodes.KEY_MINUS, False),
            "…": (ecodes.KEY_DOT, False),
            "\u00a0": (ecodes.KEY_SPACE, False),
            "«": (ecodes.KEY_COMMA, True),
            "»": (ecodes.KEY_DOT, True),
        }
        for ch, (exp_code, exp_shift) in expected.items():
            code, shift = remoteu.resolve_key(ch)
            self.assertEqual(code, exp_code, f"Failed for character {ch!r}")
            self.assertEqual(shift, exp_shift, f"Failed shift for {ch!r}")


from aiohttp.test_utils import AioHTTPTestCase
import json


class TestWebSocketIntegration(AioHTTPTestCase):
    async def get_application(self):
        return remoteu.create_app()

    async def test_http_index(self):
        """Test GET / returns HTML template with 200 OK."""
        resp = await self.client.get("/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("Hypr-Remote", text)
        self.assertIn("/ws", text)
        self.assertIn("settingsModal", text)
        self.assertIn("calculateBallistics", text)
        self.assertIn("sensitivitySlider", text)
        self.assertIn("visualViewport", text)
        self.assertIn("kbTrigger", text)
        self.assertIn("updateLatencyIndicator", text)
        self.assertIn("pingPacket", text)
        self.assertIn("getCoalescedEvents", text)
        self.assertIn("pointerId", text)
        self.assertIn("motionSmoothToggle", text)
        self.assertIn("motionSmooth", text)

    async def test_ws_connection_and_messages(self):
        """Test WebSocket text and key combos are processed in order."""
        events = []
        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = lambda ev_type, code, value: events.append(
                ("down" if value else "up", code)
            )
            remoteu.ui.syn = lambda: None

            ws = await self.client.ws_connect("/ws")
            msg = await ws.receive_json()
            self.assertEqual(msg, {"type": "connected", "status": "ready"})

            await ws.send_str(json.dumps({"type": "type_text", "text": "Hi"}))
            await ws.send_str(json.dumps({"type": "key_combo", "modifiers": [], "key": "escape"}))
            await asyncio.sleep(0.01)

            self.assertEqual(
                events,
                [
                    ("down", ecodes.KEY_LEFTSHIFT),
                    ("down", ecodes.KEY_H),
                    ("up", ecodes.KEY_H),
                    ("up", ecodes.KEY_LEFTSHIFT),
                    ("down", ecodes.KEY_I),
                    ("up", ecodes.KEY_I),
                    ("down", ecodes.KEY_ESC),
                    ("up", ecodes.KEY_ESC),
                ],
            )
            await ws.close()
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn


if __name__ == "__main__":
    unittest.main()
