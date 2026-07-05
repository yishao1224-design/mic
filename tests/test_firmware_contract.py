from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "Micophone" / "Micophone.ino"


class FirmwareContractTests(unittest.TestCase):
    def test_firmware_does_not_gate_audio_recording_locally(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertNotIn("recordingEnabled", source)
        self.assertNotIn("RECORDING_ON_PACKET", source)
        self.assertNotIn("RECORDING_OFF_PACKET", source)
        self.assertNotIn("recordingStateNotifyPending", source)

    def test_button_click_only_queues_right_alt_marker(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("if (M5.BtnA.wasClicked())", source)
        self.assertIn("rightAltTapPending = deviceConnected;", source)
        self.assertNotIn("recordingEnabled = !recordingEnabled", source)

    def test_button_hold_queues_enter_marker(self):
        source = FIRMWARE.read_text(encoding="utf-8")

        self.assertIn("ENTER_TAP_PACKET", source)
        self.assertIn("enterTapPending", source)
        self.assertIn("if (M5.BtnA.wasHold())", source)
        self.assertIn("enterTapPending = deviceConnected;", source)


if __name__ == "__main__":
    unittest.main()
