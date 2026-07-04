# Right Alt Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a Windows Right Alt tap from the host when the active M5 firmware reports an M5 button click.

**Architecture:** Use the existing BLE notify characteristic for a distinct `M5KRA` control marker. Parse that marker in `main.py` before audio/legacy short-packet handling and isolate Windows `SendInput` behind a helper function for testability.

**Tech Stack:** Python `unittest`, Windows `ctypes`, Arduino/M5Unified BLE notifications.

---

### Task 1: Python Control Marker

**Files:**
- Create: `tests/test_button_control.py`
- Modify: `main.py`

- [ ] **Step 1: Write the failing test**

```python
def test_button_marker_taps_right_alt_once(self):
    calls = []
    self.main.tap_right_alt = lambda: calls.append("tap")

    self.main.notification_handler("char", self.main.RIGHT_ALT_TAP_PACKET)

    self.assertEqual(calls, ["tap"])
    self.assertEqual(self.main.packet_counter, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m unittest tests.test_button_control -v`

Expected: fail because `RIGHT_ALT_TAP_PACKET` or the marker handling does not exist.

- [ ] **Step 3: Write minimal implementation**

Add `RIGHT_ALT_TAP_PACKET = b"M5KRA"`, `tap_right_alt()`, and an early branch in `notification_handler()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m unittest tests.test_button_control -v`

Expected: pass.

### Task 2: Firmware Button Marker

**Files:**
- Modify: `Micophone/Micophone.ino`

- [ ] **Step 1: Add marker constant**

```cpp
static const uint8_t RIGHT_ALT_TAP_PACKET[] = {'M', '5', 'K', 'R', 'A'};
```

- [ ] **Step 2: Notify on click**

```cpp
if (deviceConnected && M5.BtnA.wasClicked()) {
    pCharacteristic->setValue((uint8_t *)RIGHT_ALT_TAP_PACKET, sizeof(RIGHT_ALT_TAP_PACKET));
    pCharacteristic->notify();
    Serial.println("button click: Right Alt tap marker sent");
}
```

- [ ] **Step 3: Verify Python tests still pass**

Run: `py -m unittest tests.test_button_control -v`

Expected: pass.
