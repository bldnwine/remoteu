import asyncio
import struct
import unittest
from aiohttp.test_utils import AioHTTPTestCase
from evdev import ecodes
import remoteu


class TestBinaryProtocol(unittest.TestCase):
    def test_mouse_delta_unpack(self):
        """Test packing and unpacking of standard 6-byte mouse delta frames (!BhhB)."""
        # Normal move: dx=120, dy=-45, drag=0
        packet = struct.pack("!BhhB", 1, 120, -45, 0)
        self.assertEqual(len(packet), 6)
        type_id, dx, dy, flags = struct.unpack("!BhhB", packet)
        self.assertEqual(type_id, 1)
        self.assertEqual(dx, 120)
        self.assertEqual(dy, -45)
        self.assertEqual(flags, 0)

        # Drag move: dx=-500, dy=250, drag=1
        packet_drag = struct.pack("!BhhB", 1, -500, 250, 1)
        type_id, dx, dy, flags = struct.unpack("!BhhB", packet_drag)
        self.assertEqual(type_id, 1)
        self.assertEqual(dx, -500)
        self.assertEqual(dy, 250)
        self.assertEqual(flags, 1)

    def test_mouse_delta_boundary_values(self):
        """Ensure boundary values (±32767, 0) pack and unpack without overflow."""
        packet_max = struct.pack("!BhhB", 1, 32767, -32767, 1)
        _, dx, dy, flags = struct.unpack("!BhhB", packet_max)
        self.assertEqual(dx, 32767)
        self.assertEqual(dy, -32767)
        self.assertEqual(flags, 1)

        packet_zero = struct.pack("!BhhB", 1, 0, 0, 0)
        _, dx, dy, flags = struct.unpack("!BhhB", packet_zero)
        self.assertEqual(dx, 0)
        self.assertEqual(dy, 0)
        self.assertEqual(flags, 0)

    def test_scroll_unpack(self):
        """Test packing and unpacking of 3-byte scroll frames (!Bh)."""
        # Scroll up (+3)
        packet_up = struct.pack("!Bh", 2, 3)
        self.assertEqual(len(packet_up), 3)
        type_id, amount = struct.unpack("!Bh", packet_up)
        self.assertEqual(type_id, 2)
        self.assertEqual(amount, 3)

        # Scroll down (-5)
        packet_down = struct.pack("!Bh", 2, -5)
        type_id, amount = struct.unpack("!Bh", packet_down)
        self.assertEqual(type_id, 2)
        self.assertEqual(amount, -5)

    def test_scroll_boundary_values(self):
        """Ensure boundary values for scroll pack and unpack cleanly."""
        packet_max = struct.pack("!Bh", 2, 32767)
        _, amount = struct.unpack("!Bh", packet_max)
        self.assertEqual(amount, 32767)

        packet_min = struct.pack("!Bh", 2, -32767)
        _, amount = struct.unpack("!Bh", packet_min)
        self.assertEqual(amount, -32767)

    def test_sequenced_mouse_unpack(self):
        """Test 12-byte sequenced mouse frames (!BHIhhB)."""
        packet = struct.pack("!BHIhhB", 4, 42, 12345678, 120, -45, 1)
        self.assertEqual(len(packet), 12)
        type_id, seq, cts, dx, dy, flags = remoteu._UNPACK_MOUSE_SEQ(packet)
        self.assertEqual((type_id, seq, cts, dx, dy, flags), (4, 42, 12345678, 120, -45, 1))

    def test_sequenced_scroll_unpack(self):
        """Test 9-byte sequenced scroll frames (!BHIh)."""
        packet = struct.pack("!BHIh", 5, 7, 999, -5)
        self.assertEqual(len(packet), 9)
        type_id, seq, cts, amount = remoteu._UNPACK_SCROLL_SEQ(packet)
        self.assertEqual((type_id, seq, cts, amount), (5, 7, 999, -5))

    def test_ping_unpack(self):
        """Test packing and unpacking of 5-byte ping/pong frames (!BI)."""
        packet = struct.pack("!BI", 3, 12345678)
        self.assertEqual(len(packet), 5)
        type_id, timestamp = struct.unpack("!BI", packet)
        self.assertEqual(type_id, 3)
        self.assertEqual(timestamp, 12345678)

    def test_input_event_struct_layout(self):
        """Ensure native Linux input_event struct layout (@llHHi) matches expected architecture size."""
        import ctypes
        expected_size = 24 if ctypes.sizeof(ctypes.c_long) == 8 else 16
        self.assertEqual(remoteu._INPUT_EVENT_STRUCT.size, expected_size)

    def test_batched_input_event_generation(self):
        """Test emit_rel_xy batches REL_X, REL_Y, and SYN_REPORT into a single write buffer."""
        written_buffers = []
        original_write = remoteu.os.write

        def mock_os_write(fd, buf):
            written_buffers.append(bytes(buf))
            return len(buf)

        try:
            remoteu.os.write = mock_os_write
            remoteu.ui.emit_rel_xy(15, -25)
            self.assertEqual(len(written_buffers), 1)
            buf = written_buffers[0]
            event_size = remoteu._INPUT_EVENT_STRUCT.size
            self.assertEqual(len(buf), 3 * event_size)

            ev1 = remoteu._INPUT_EVENT_STRUCT.unpack(buf[:event_size])
            ev2 = remoteu._INPUT_EVENT_STRUCT.unpack(buf[event_size:2*event_size])
            ev3 = remoteu._INPUT_EVENT_STRUCT.unpack(buf[2*event_size:])

            self.assertEqual(ev1[2], ecodes.EV_REL)
            self.assertEqual(ev1[3], ecodes.REL_X)
            self.assertEqual(ev1[4], 15)

            self.assertEqual(ev2[2], ecodes.EV_REL)
            self.assertEqual(ev2[3], ecodes.REL_Y)
            self.assertEqual(ev2[4], -25)

            self.assertEqual(ev3[2], ecodes.EV_SYN)
            self.assertEqual(ev3[3], ecodes.SYN_REPORT)
            self.assertEqual(ev3[4], 0)
        finally:
            remoteu.os.write = original_write

    def test_emit_click_batches_down_up_and_report(self):
        """Test click injection uses one write with explicit transition reports."""
        written_buffers = []
        original_write = remoteu.os.write

        def mock_os_write(fd, buf):
            written_buffers.append(bytes(buf))
            return len(buf)

        try:
            remoteu.os.write = mock_os_write
            remoteu.emit_click(ecodes.BTN_LEFT)
            self.assertEqual(len(written_buffers), 1)
            event_size = remoteu._INPUT_EVENT_STRUCT.size
            self.assertEqual(len(written_buffers[0]), 4 * event_size)
            events = [
                remoteu._INPUT_EVENT_STRUCT.unpack_from(written_buffers[0], i * event_size)
                for i in range(4)
            ]
            self.assertEqual(events[0][2:5], (ecodes.EV_KEY, ecodes.BTN_LEFT, 1))
            self.assertEqual(events[1][2:5], (ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
            self.assertEqual(events[2][2:5], (ecodes.EV_KEY, ecodes.BTN_LEFT, 0))
            self.assertEqual(events[3][2:5], (ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
        finally:
            remoteu.os.write = original_write

    def test_motion_and_scroll_share_one_report(self):
        written_buffers = []
        original_write = remoteu.os.write

        def mock_os_write(fd, buf):
            written_buffers.append(bytes(buf))
            return len(buf)

        try:
            remoteu.os.write = mock_os_write
            remoteu.ui.emit_rel_scroll(15, -25, 3)
            self.assertEqual(len(written_buffers), 1)
            event_size = remoteu._INPUT_EVENT_STRUCT.size
            self.assertEqual(len(written_buffers[0]), 5 * event_size)
            events = [
                remoteu._INPUT_EVENT_STRUCT.unpack_from(written_buffers[0], i * event_size)
                for i in range(5)
            ]
            self.assertEqual(
                [(event[2], event[3], event[4]) for event in events],
                [
                    (ecodes.EV_REL, ecodes.REL_X, 15),
                    (ecodes.EV_REL, ecodes.REL_Y, -25),
                    (ecodes.EV_REL, ecodes.REL_WHEEL, 3),
                    (ecodes.EV_REL, ecodes.REL_WHEEL_HI_RES, 360),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                ],
            )
        finally:
            remoteu.os.write = original_write

    def test_text_burst_batches_all_key_transitions(self):
        written_buffers = []
        original_write = remoteu.os.write

        def mock_os_write(fd, buf):
            written_buffers.append(bytes(buf))
            return len(buf)

        try:
            remoteu.os.write = mock_os_write
            remoteu.handle_type_text({"text": "Aa"})
            self.assertEqual(len(written_buffers), 1)
            event_size = remoteu._INPUT_EVENT_STRUCT.size
            self.assertEqual(len(written_buffers[0]), 12 * event_size)
            events = [
                remoteu._INPUT_EVENT_STRUCT.unpack_from(written_buffers[0], i * event_size)
                for i in range(12)
            ]
            self.assertEqual(
                [(event[2], event[3], event[4]) for event in events],
                [
                    (ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 1),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                    (ecodes.EV_KEY, ecodes.KEY_A, 1),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                    (ecodes.EV_KEY, ecodes.KEY_A, 0),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                    (ecodes.EV_KEY, ecodes.KEY_LEFTSHIFT, 0),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                    (ecodes.EV_KEY, ecodes.KEY_A, 1),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                    (ecodes.EV_KEY, ecodes.KEY_A, 0),
                    (ecodes.EV_SYN, ecodes.SYN_REPORT, 0),
                ],
            )
        finally:
            remoteu.os.write = original_write

    def test_scroll_emits_legacy_and_high_resolution_events(self):
        written_buffers = []
        original_write = remoteu.os.write

        def mock_os_write(fd, buf):
            written_buffers.append(bytes(buf))
            return len(buf)

        try:
            remoteu.os.write = mock_os_write
            remoteu.ui.emit_scroll(3)
            self.assertEqual(len(written_buffers), 1)
            event_size = remoteu._INPUT_EVENT_STRUCT.size
            self.assertEqual(len(written_buffers[0]), 3 * event_size)
            events = [
                remoteu._INPUT_EVENT_STRUCT.unpack_from(written_buffers[0], i * event_size)
                for i in range(3)
            ]
            self.assertEqual(events[0][2:5], (ecodes.EV_REL, ecodes.REL_WHEEL, 3))
            self.assertEqual(
                events[1][2:5],
                (ecodes.EV_REL, ecodes.REL_WHEEL_HI_RES, 360),
            )
            self.assertEqual(events[2][2:5], (ecodes.EV_SYN, ecodes.SYN_REPORT, 0))
        finally:
            remoteu.os.write = original_write


class TestMotionStats(unittest.TestCase):
    def test_seq_gaps_and_wrap(self):
        now = [100.0]
        stats = remoteu.MotionStats(time_fn=lambda: now[0])
        stats.record(65534, 1000, 5, 0)
        stats.record(65535, 1008, 5, 0)
        stats.record(1, 1016, 5, 0)  # wrapped; seq 0 missing
        self.assertEqual(stats.frames, 3)
        self.assertEqual(stats.sequenced_frames, 3)
        self.assertEqual(stats.seq_gaps, 1)

    def test_jitter_stalls_teleports(self):
        now = [100.0]
        stats = remoteu.MotionStats(time_fn=lambda: now[0])
        stats.record(0, 1000, 5, 0)
        now[0] += 0.008
        stats.record(1, 1008, 5, 0)  # on-time: jitter stays ~0
        self.assertAlmostEqual(stats.jitter_ms, 0.0)
        now[0] += 0.060  # RF stall
        stats.record(2, 1016, 5, 0)
        self.assertEqual(stats.stalls, 1)
        self.assertGreater(stats.jitter_ms, 1.0)
        now[0] += 0.008
        stats.record(3, 1024, 150, 100)  # burst teleport
        self.assertEqual(stats.teleports, 1)
        self.assertEqual(stats.max_burst_px, 250)
        snap = stats.snapshot()
        self.assertEqual(snap["frames"], 4)

    def test_legacy_frames_count_without_seq(self):
        stats = remoteu.MotionStats(time_fn=lambda: 0.0)
        stats.record(None, None, 3, -2)
        self.assertEqual(stats.frames, 1)
        self.assertEqual(stats.sequenced_frames, 0)
        self.assertEqual(stats.seq_gaps, 0)


class TestMotionSmoother(unittest.TestCase):
    def test_direct_mode_passes_through(self):
        emitted = []
        smoother = remoteu.MotionSmoother(
            emit=lambda dx, dy, s: emitted.append((dx, dy, s)),
            time_fn=lambda: 0.0,
            enabled=False,
        )
        smoother.push(10, -5, 2)
        self.assertEqual(emitted, [(10, -5, 2)])

    def test_smooth_is_default(self):
        smoother = remoteu.MotionSmoother(emit=lambda dx, dy, s: None)
        self.assertTrue(smoother.enabled)

    def test_smooth_mode_holds_then_releases(self):
        now = [100.0]
        emitted = []
        smoother = remoteu.MotionSmoother(
            emit=lambda dx, dy, s: emitted.append((dx, dy, s)),
            time_fn=lambda: now[0],
        )
        smoother.set_jitter(10.0)  # playout delay 20ms
        smoother.set_enabled(True)
        smoother.push(10, 0)
        smoother.tick()
        self.assertEqual(emitted, [])
        now[0] += 0.025
        smoother.tick()
        self.assertEqual(emitted, [(10, 0, 0)])

    def test_disable_flushes_queue(self):
        emitted = []
        smoother = remoteu.MotionSmoother(
            emit=lambda dx, dy, s: emitted.append((dx, dy, s)),
            time_fn=lambda: 100.0,
        )
        smoother.set_jitter(10.0)
        smoother.set_enabled(True)
        smoother.push(7, 3)
        smoother.set_enabled(False)
        self.assertEqual(emitted, [(7, 3, 0)])

    def test_prediction_bounded_during_stall(self):
        now = [100.0]
        emitted = []
        smoother = remoteu.MotionSmoother(
            emit=lambda dx, dy, s: emitted.append((dx, dy, s)),
            time_fn=lambda: now[0],
        )
        smoother.set_enabled(True)
        # Build velocity with a fast motion, then stall.
        smoother.push(80, 0)
        now[0] += 0.008
        smoother.tick()  # emits, tracks velocity
        self.assertEqual(len(emitted), 1)
        for _ in range(30):
            now[0] += 0.008
            smoother.tick()
        predicted = sum(abs(dx) + abs(dy) for dx, dy, _ in emitted[1:])
        self.assertGreater(len(emitted), 1)  # prediction fired
        self.assertLessEqual(predicted, 96 + 16)  # bounded + one step


class TestCommandExecution(unittest.IsolatedAsyncioTestCase):
    class FakeProcess:
        def __init__(self, communicate):
            self.returncode = None
            self.killed = False
            self.waited = False
            self._communicate = communicate

        async def communicate(self):
            await self._communicate()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return self.returncode

    async def test_run_cmd_kills_and_reaps_timed_out_process(self):
        async def timeout():
            raise asyncio.TimeoutError

        proc = self.FakeProcess(timeout)
        create_process = unittest.mock.AsyncMock(return_value=proc)
        with unittest.mock.patch.object(remoteu.asyncio, "create_subprocess_exec", create_process):
            success, message = await remoteu.run_cmd("slow-command")

        self.assertFalse(success)
        self.assertEqual(message, "slow-command timed out")
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)

    async def test_run_cmd_kills_and_reaps_cancelled_process(self):
        started = asyncio.Event()
        blocked = asyncio.Event()

        async def wait_forever():
            started.set()
            await blocked.wait()

        proc = self.FakeProcess(wait_forever)
        create_process = unittest.mock.AsyncMock(return_value=proc)
        with unittest.mock.patch.object(remoteu.asyncio, "create_subprocess_exec", create_process):
            task = asyncio.create_task(remoteu.run_cmd("slow-command"))
            await asyncio.wait_for(started.wait(), timeout=0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(proc.killed)
        self.assertTrue(proc.waited)


class TestWebSocketBinaryIntegration(AioHTTPTestCase):
    async def get_application(self):
        return remoteu.create_app()

    async def test_slow_command_does_not_delay_ping_pong(self):
        command_started = asyncio.Event()
        release_command = asyncio.Event()

        async def slow_run_cmd(*args):
            command_started.set()
            await release_command.wait()
            return True, ""

        original_run_cmd = remoteu.run_cmd
        remoteu.run_cmd = slow_run_cmd
        ws = None
        try:
            ws = await self.client.ws_connect("/ws")
            await ws.receive_json()
            await ws.send_str('{"type":"command","name":"volume_up","params":{}}')
            await asyncio.wait_for(command_started.wait(), timeout=0.1)

            ping = struct.pack("!BI", 3, 12345)
            await ws.send_bytes(ping)
            pong = await asyncio.wait_for(ws.receive_bytes(), timeout=0.1)
            self.assertEqual(pong, ping)

            release_command.set()
            await asyncio.sleep(0)
            await ws.close()
            ws = None
        finally:
            release_command.set()
            remoteu.run_cmd = original_run_cmd
            if ws is not None:
                await ws.close()

    async def test_ws_binary_mouse_delta(self):
        """Test WebSocket client streaming 6-byte binary mouse delta frames."""
        written_events = []

        def mock_write(ev_type, code, value):
            written_events.append((ev_type, code, value))

        syn_count = [0]

        def mock_syn():
            syn_count[0] += 1

        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = mock_write
            remoteu.ui.syn = mock_syn

            ws = await self.client.ws_connect("/ws")
            msg = await ws.receive_json()
            self.assertEqual(msg, {"type": "connected", "status": "ready"})

            # Send binary mouse delta frame (dx=15, dy=-25, drag=0)
            packet = struct.pack("!BhhB", 1, 15, -25, 0)
            await ws.send_bytes(packet)

            import asyncio
            await asyncio.sleep(0.02)
            await ws.close()

            expected = [
                (ecodes.EV_REL, ecodes.REL_X, 15),
                (ecodes.EV_REL, ecodes.REL_Y, -25),
            ]
            self.assertEqual(written_events, expected)
            self.assertGreaterEqual(syn_count[0], 1)
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn

    async def test_ws_binary_scroll(self):
        """Test WebSocket client streaming 3-byte binary scroll frames."""
        written_events = []

        def mock_write(ev_type, code, value):
            written_events.append((ev_type, code, value))

        syn_count = [0]

        def mock_syn():
            syn_count[0] += 1

        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = mock_write
            remoteu.ui.syn = mock_syn

            ws = await self.client.ws_connect("/ws")
            await ws.receive_json()

            # Send binary scroll frame (amount=3 -> scroll up)
            packet = struct.pack("!Bh", 2, 3)
            await ws.send_bytes(packet)

            import asyncio
            await asyncio.sleep(0.02)
            await ws.close()

            expected = [
                (ecodes.EV_REL, ecodes.REL_WHEEL, 3),
                (ecodes.EV_REL, ecodes.REL_WHEEL_HI_RES, 360),
            ]
            self.assertEqual(written_events, expected)
            self.assertGreaterEqual(syn_count[0], 1)
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn

    async def test_ws_binary_malformed_ignored(self):
        """Ensure malformed binary frames (incorrect length or unknown type) are safely discarded."""
        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = unittest.mock.Mock()
            remoteu.ui.syn = unittest.mock.Mock()

            ws = await self.client.ws_connect("/ws")
            await ws.receive_json()

            # Send invalid binary frames
            await ws.send_bytes(b"\x01\x00")  # 2 bytes (too short)
            await ws.send_bytes(b"\x01\x00\x00\x00\x00")  # 5 bytes
            await ws.send_bytes(b"\x01\x00\x00\x00\x00\x00\x00")  # 7 bytes
            await ws.send_bytes(b"\x99\x00\x00")  # unknown type ID
            await ws.send_bytes(b"")  # empty

            # Send valid JSON message afterwards to prove socket connection is intact and uncorrupted
            await ws.send_str('{"type":"key_combo","modifiers":[],"key":"escape"}')

            await ws.close()
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn

    async def test_ws_error_handling_keeps_connection_alive(self):
        """Ensure exceptions during input processing send error JSON and do not disconnect WS."""
        def mock_failing_write(ev_type, code, value):
            raise OSError("Simulated UInput device error")

        orig_write = remoteu.ui.write
        try:
            remoteu.ui.write = mock_failing_write

            ws = await self.client.ws_connect("/ws")
            msg = await ws.receive_json()
            self.assertEqual(msg, {"type": "connected", "status": "ready"})

            # Send binary mouse delta frame which triggers failing write
            packet = struct.pack("!BhhB", 1, 10, 10, 0)
            await ws.send_bytes(packet)

            err_msg = await ws.receive_json()
            self.assertEqual(err_msg["type"], "error")
            self.assertIn("Simulated UInput device error", err_msg["message"])

            # Verify WebSocket is still open and responsive
            self.assertFalse(ws.closed)

            await ws.close()
        finally:
            remoteu.ui.write = orig_write

    async def test_ws_binary_ping_pong(self):
        """Test WebSocket server echoes 5-byte ping frame back as pong."""
        ws = await self.client.ws_connect("/ws")
        await ws.receive_json()

        # Send 5-byte ping frame (type 3, timestamp 54321)
        ping_packet = struct.pack("!BI", 3, 54321)
        await ws.send_bytes(ping_packet)

        pong_msg = await ws.receive()
        self.assertEqual(pong_msg.data, ping_packet)
        await ws.close()

    async def test_ws_binary_mouse_delta_coalescing(self):
        """Test server coalesces multiple queued binary delta frames into a single write+syn."""
        written_events = []

        def mock_write(ev_type, code, value):
            written_events.append((ev_type, code, value))

        syn_count = [0]

        def mock_syn():
            syn_count[0] += 1

        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = mock_write
            remoteu.ui.syn = mock_syn

            ws = await self.client.ws_connect("/ws")
            await ws.receive_json()

            # Send two binary frames back to back
            packet1 = struct.pack("!BhhB", 1, 10, 20, 0)
            packet2 = struct.pack("!BhhB", 1, 15, -5, 0)
            await ws.send_bytes(packet1)
            await ws.send_bytes(packet2)

            import asyncio
            await asyncio.sleep(0.02)
            await ws.close()

            # The total movement across packets must be 25 in X and 15 in Y
            total_x = sum(v for t, c, v in written_events if c == ecodes.REL_X)
            total_y = sum(v for t, c, v in written_events if c == ecodes.REL_Y)
            self.assertEqual(total_x, 25)
            self.assertEqual(total_y, 15)
            self.assertGreaterEqual(syn_count[0], 1)
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn


    async def test_ws_sequenced_frames_and_motion_mode(self):
        """Test sequenced frames inject like legacy ones; stats/mode JSON works."""
        import json

        written_events = []

        def mock_write(ev_type, code, value):
            written_events.append((ev_type, code, value))

        syn_count = [0]

        def mock_syn():
            syn_count[0] += 1

        orig_write = remoteu.ui.write
        orig_syn = remoteu.ui.syn
        try:
            remoteu.ui.write = mock_write
            remoteu.ui.syn = mock_syn

            ws = await self.client.ws_connect("/ws")
            await ws.receive_json()

            packet = struct.pack("!BHIhhB", 4, 10, 5000, 15, -25, 0)
            await ws.send_bytes(packet)
            await asyncio.sleep(0.02)

            self.assertIn((ecodes.EV_REL, ecodes.REL_X, 15), written_events)
            self.assertIn((ecodes.EV_REL, ecodes.REL_Y, -25), written_events)

            await ws.send_str(json.dumps({"type": "motion_stats"}))
            stats_msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
            self.assertEqual(stats_msg["type"], "motion_stats")
            self.assertGreaterEqual(stats_msg["stats"]["frames"], 1)
            self.assertGreaterEqual(stats_msg["stats"]["sequenced_frames"], 1)

            await ws.send_str(json.dumps({"type": "motion_mode", "mode": "smooth"}))
            mode_msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
            self.assertEqual(mode_msg, {"type": "motion_mode", "mode": "smooth"})

            packet2 = struct.pack("!BHIhhB", 4, 11, 5008, 4, 4, 0)
            await ws.send_bytes(packet2)
            await asyncio.sleep(0.05)

            await ws.send_str(json.dumps({"type": "motion_mode", "mode": "direct"}))
            mode_msg = await asyncio.wait_for(ws.receive_json(), timeout=1.0)
            self.assertEqual(mode_msg, {"type": "motion_mode", "mode": "direct"})

            await ws.close()
        finally:
            remoteu.ui.write = orig_write
            remoteu.ui.syn = orig_syn


if __name__ == "__main__":
    unittest.main()

