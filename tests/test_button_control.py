import importlib
import io
import sys
import types
import unittest
from contextlib import redirect_stdout


def install_dependency_fakes():
    bleak = types.ModuleType("bleak")
    bleak.BleakScanner = object
    bleak.BleakClient = object

    bleak_exc = types.ModuleType("bleak.exc")

    class BleakError(Exception):
        pass

    bleak_exc.BleakError = BleakError

    bleak_backends = types.ModuleType("bleak.backends")
    bleak_winrt = types.ModuleType("bleak.backends.winrt")
    bleak_util = types.ModuleType("bleak.backends.winrt.util")
    bleak_util.uninitialize_sta = lambda: None
    bleak_util.assert_mta = None
    bleak_util.allow_sta = None

    sounddevice = types.ModuleType("sounddevice")
    sounddevice.play = lambda *args, **kwargs: None

    sys.modules.setdefault("bleak", bleak)
    sys.modules.setdefault("bleak.exc", bleak_exc)
    sys.modules.setdefault("bleak.backends", bleak_backends)
    sys.modules.setdefault("bleak.backends.winrt", bleak_winrt)
    sys.modules.setdefault("bleak.backends.winrt.util", bleak_util)
    sys.modules.setdefault("sounddevice", sounddevice)


class ButtonControlTests(unittest.TestCase):
    def setUp(self):
        install_dependency_fakes()
        sys.modules.pop("main", None)
        self.main = importlib.import_module("main")
        self.main.stream = object()

    def test_sendinput_layout_matches_windows_input_size(self):
        expected_min_size = 40 if sys.maxsize > 2**32 else 28

        self.assertGreaterEqual(self.main.input_structure_size(), expected_min_size)


    def test_button_marker_taps_right_alt_once(self):
        calls = []
        self.main.start_right_alt_tap = lambda: calls.append("tap")

        with redirect_stdout(io.StringIO()):
            self.main.notification_handler("char", self.main.RIGHT_ALT_TAP_PACKET)

        self.assertEqual(calls, ["tap"])
        self.assertEqual(self.main.packet_counter, 1)

    def test_button_marker_does_not_raise_when_key_tap_fails(self):
        def fail_tap():
            raise OSError(87, "invalid parameter")

        self.main.start_right_alt_tap = fail_tap

        with redirect_stdout(io.StringIO()):
            self.main.notification_handler("char", self.main.RIGHT_ALT_TAP_PACKET)

        self.assertEqual(self.main.packet_counter, 1)


    def test_button_marker_is_not_queued_as_audio_or_test_tone(self):
        taps = []
        queued = []
        self.main.start_right_alt_tap = lambda: taps.append("tap")
        self.main.enqueue_playback = lambda samples: queued.append(samples)

        with redirect_stdout(io.StringIO()):
            self.main.notification_handler("char", self.main.RIGHT_ALT_TAP_PACKET)

        self.assertEqual(taps, ["tap"])
        self.assertEqual(queued, [])
        self.assertFalse(self.main.warned_non_audio)


if __name__ == "__main__":
    unittest.main()
