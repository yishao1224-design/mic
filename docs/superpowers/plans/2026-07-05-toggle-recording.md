# Toggle Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each M5 button click tap Right Alt and toggle whether the device records/transmits audio.

**Architecture:** Firmware keeps BLE connected but gates microphone recording and PCM notify behind `recordingEnabled`. Button clicks queue a Right Alt marker plus a recording state marker; the mic task sends control markers even while audio is idle, then only records when enabled. Python recognizes `M5REC`/`M5IDL` markers and suppresses stall recovery while the device reports idle.

**Tech Stack:** Arduino M5Unified BLE firmware, Python bleak client, unittest.

---

### Task 1: Python Recording State Markers

**Files:**
- Modify: `main.py`
- Modify: `tests/test_button_control.py`

- [ ] Write failing tests for `RECORDING_ON_PACKET`, `RECORDING_OFF_PACKET`, and idle stall suppression.
- [ ] Implement packet constants, `device_recording_enabled`, and `should_recover_stall()`.
- [ ] Handle status marker packets before audio/counter parsing.
- [ ] Run `python -m unittest tests.test_button_control -v`.

### Task 2: Firmware Toggle State Machine

**Files:**
- Modify: `Micophone/Micophone.ino`

- [ ] Add `recordingEnabled`, `controlNotifyPending`, and status packet constants.
- [ ] On every `BtnA.wasClicked()`, toggle recording, queue Right Alt + status markers, and update the display.
- [ ] In `mic_record_task`, send queued control markers even while idle.
- [ ] When idle, do not call `M5.Mic.record()` or send PCM audio.
- [ ] When recording starts, prime the double buffer and stream until toggled off.
- [ ] Compile and upload to `COM3` at `upload.speed=115200`.
