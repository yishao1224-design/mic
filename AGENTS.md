# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What this is

A BLE audio streaming pipeline: an M5Stick-C Plus (ESP32) captures microphone audio and streams it over
Bluetooth Low Energy as GATT notifications; a Windows Python client (`main.py`) scans for the device,
connects, subscribes to the audio characteristic, resamples the PCM stream, and plays it out through a
local audio device (optionally a VB-Cable virtual output). There is no build system, package manifest,
or test suite — this is a two-piece hardware/host script project.

## Repository layout

- `main.py` — the Python BLE client/receiver. This is where almost all the logic and edge-case handling lives.
- `mic.ino` — firmware using raw ESP-IDF `i2s` driver + `M5StickCPlus` library (older/lower-level approach).
- `Micophone/Micophone.ino` — firmware using `M5Unified`'s higher-level `M5.Mic` API at 8kHz (current, actively developed variant).
- `Micophone_official/Micophone_official.ino` — a stripped-down mic capture reference sketch (no BLE), used for isolating mic-only issues.
- `requirements.txt` — Python deps: `bleak`, `numpy`, `sounddevice`.
- `M5Unified/` — empty placeholder directory (not a real dependency checkout; the Arduino library must be installed separately via Arduino IDE/PlatformIO library manager).

Only one `.ino` firmware is flashed to the device at a time. When making protocol changes (UUIDs, packet
size, sample rate), check which sketch is actually deployed — `Micophone/Micophone.ino` is the one under
active iteration per recent commits, but `mic.ino` may still be referenced by older devices/notes.

## Running the Python client

```bash
pip install -r requirements.txt
python main.py
```

Runs on Windows only (uses `bleak`'s WinRT backend and Windows-specific STA/MTA handling in
`prepare_windows_ble()`). For audio to route into other apps (Discord, OBS, etc.) rather than the
speakers, install VB-Cable — `main.py` auto-detects an output device whose name contains "CABLE Input"
and falls back to the default speaker otherwise.

There are no automated tests; validation is manual (run the script against real hardware and listen for
audio, or watch the printed diagnostics).
## Arduino CLI

Arduino CLI is installed on this Windows machine via `winget`:

- Executable: `C:\Program Files\Arduino CLI\arduino-cli.exe`
- Verified version: `arduino-cli  Version: 1.5.1`

The installer added `C:\Program Files\Arduino CLI\` to the machine `PATH`; new PowerShell/CMD windows
should be able to run `arduino-cli` directly. Existing shells may need their `PATH` refreshed first.

When flashing `Micophone/Micophone.ino` to the M5StickC Plus, use the M5StickC Plus FQBN and force a
conservative upload baud rate. The default high-speed upload attempted `1500000` baud and connected to
the ESP32-PICO-D4, but the chip stopped responding before flash write. `115200` baud was verified to
compile, upload, and verify successfully on `COM3`:

```powershell
& 'C:\Program Files\Arduino CLI\arduino-cli.exe' compile --fqbn esp32:esp32:m5stack_stickc_plus --build-path C:\tmp\mic-build .\Micophone
& 'C:\Program Files\Arduino CLI\arduino-cli.exe' upload --fqbn esp32:esp32:m5stack_stickc_plus -p COM3 --input-dir C:\tmp\mic-build --upload-property upload.speed=115200 .\Micophone
```

## BLE protocol contract (must stay in sync across `main.py` and the `.ino` firmware)

- Service UUID: `4fafc201-1fb5-459e-8fcc-c5c9c331914b` (a second legacy UUID, `19b10000-...`, is also
  accepted by the scanner for backward compatibility).
- Characteristic UUID: `beb5483e-36e1-4688-b7f5-ea07361b26a8` (READ + NOTIFY).
- Device name: `M5_BLE_Mic` (or `M5_Mic_A`) — `main.py` matches on both service UUID and name.
- Payload: raw little-endian mono `int16` PCM. Packets of length <= 4 bytes are treated as legacy
  "counter" test packets (not audio) and are converted into a synthetic tone so the output path can still
  be sanity-checked without real audio.
- Input sample rate assumed by `main.py` is `input_sample_rate = 8000` (must match the firmware's
  `MIC_SAMPLE_RATE`); output is upsampled to 16000 Hz for playback. If firmware sample rate or packet size
  changes, update the corresponding constants in `main.py` (`input_sample_rate`, `READ_SAMPLES`/`READ_LEN` on the device side).

## `main.py` architecture

The client is a single long `async main()` with heavy defensive/retry logic layered on top of a simple
BLE audio pipeline, because Windows BLE stacks (via `bleak`'s WinRT backend) are unreliable in practice:

1. **Windows STA/MTA workaround** (`prepare_windows_ble`) — forces the process into MTA mode, since WinRT
   BLE callbacks fail silently from an STA thread; falls back to `allow_sta()` if the MTA assertion fails.
2. **Device discovery** (`find_target_device`) — scans and matches by service UUID first, then by device
   name, printing diagnostics either way so a failed match is debuggable from the console output.
3. **Multi-strategy connect** (`connect_with_progress`, and the `winrt_profiles` loop in `main()`) — tries
   several combinations of BLE device handle vs. address string, and public/random address type, with/without
   pairing, because different Windows adapters and firmware states need different connection parameters.
   It also treats "connected state stable for 6s+" as success even if `connect()` itself hasn't returned,
   since some stacks report connection before the coroutine resolves.
4. **Notify subscription + stall recovery** — once subscribed, the main loop watches for gaps in incoming
   packets and automatically re-subscribes / actively reads the characteristic if the stream appears to
   have stalled (no packets for 5-10s).
5. **Audio pipeline** (`notification_handler`) — running DC-offset removal (a smooth estimate carried
   across packets, not per-packet mean subtraction, which clicks) → gain → linear resample from
   `input_sample_rate` to `output_sample_rate` (`resample_mono_int16`, a fractional-index interpolator that
   carries state across calls) → `enqueue_playback` into a bounded jitter buffer drained by a dedicated
   writer thread (`playback_writer_loop`). **The BLE callback must never block**: a blocking
   `stream.write()` there lets notifications pile up invisibly in the WinRT/bleak queue, and playback
   delay grows to tens of seconds (this was a real production bug). Latency is bounded by discarding the
   oldest buffered audio whenever the buffer exceeds `max_buffer_seconds`. The handler also prints the
   measured incoming sample rate every 5 s — if it deviates from `input_sample_rate`, pitch/speed are
   wrong and the buffer either grows (drops) or starves.

When debugging connection issues, the print statements (in Chinese) are intentional structured diagnostics
(step numbers like "步骤A/B/C") — read them in order, they narrate exactly which phase failed.

## Firmware architecture (`.ino` sketches)

Both `mic.ino` and `Micophone/Micophone.ino` follow the same shape:
- BLE server setup with connect/disconnect callbacks that restart advertising on disconnect.
- A dedicated FreeRTOS task (`mic_record_task`) pinned to a core, separate from the Arduino `loop()`, so
  mic capture/BLE notify isn't blocked by other work.
- On-device diagnostics: waveform drawn to the LCD, and a "silence" indicator if too many consecutive
  packets have zero peak amplitude (helps diagnose wiring/mic hardware issues independent of BLE).
- `mic.ino` reads raw I2S via the ESP-IDF driver directly (`i2s_read`); `Micophone/Micophone.ino` instead
  uses `M5.Mic.record(...)` from the M5Unified library — note `record()` is **asynchronous** (DMA fills the
  buffer in the background), so the sketch double-buffers: it queues the next capture, waits for
  `M5.Mic.isRecording() < 2` (older request finished), then streams the completed buffer. Reading the
  buffer immediately after `record()` returns sends half-filled data (garbled audio).
- `Micophone/Micophone.ino` sends 240 samples (480 bytes) per notify at 8 kHz (30 ms/packet) and calls
  `BLEDevice::setMTU(517)` so the payload fits one notification — if the MTU stays at default the payload
  is silently truncated.
