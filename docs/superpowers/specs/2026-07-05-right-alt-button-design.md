# Right Alt Button Design

## Goal

Trigger typeless audio listening on Windows by sending a Right Alt tap when the M5Stick-C Plus button is clicked.

## Protocol

The active firmware sends normal audio as raw little-endian mono int16 PCM. Button clicks will use a separate short BLE notification marker, `M5KRA`, on the existing notify characteristic. The Python client recognizes that exact marker before legacy short-packet handling, so the marker does not become a synthetic test tone.

## Host Behavior

`main.py` handles the marker by calling a small Windows key injection helper. The helper uses `SendInput` to send Right Alt down and Right Alt up with the extended-key flag. On non-Windows systems it logs and does nothing.

## Firmware Behavior

`Micophone/Micophone.ino` watches `M5.BtnA.wasClicked()` in `loop()` and records a pending tap only when BLE is connected. The mic task owns BLE notification writes; after its next audio packet it sends the marker notification and clears the pending flag. Audio streaming remains unchanged.

## Testing

Python unit tests cover that the marker triggers one key tap and that the marker is not processed as audio or as the legacy counter-test packet.
