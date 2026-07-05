import asyncio
import collections
import ctypes
import ctypes.wintypes
import sys
import threading
import time

from bleak import BleakScanner, BleakClient
from bleak.backends.winrt.util import uninitialize_sta
from bleak.exc import BleakError
import numpy as np
import sounddevice as sd

try:
    from bleak.backends.winrt.util import assert_mta
except ImportError:
    assert_mta = None

try:
    from bleak.backends.winrt.util import allow_sta
except ImportError:
    allow_sta = None

# 支持两套常见固件服务 UUID：
# 1) 自定义麦克风固件: 4faf...
# 2) 常见 Arduino BLE 示例: 19b1...
TARGET_SERVICE_UUIDS = {
    "4fafc201-1fb5-459e-8fcc-c5c9c331914b",
    "19b10000-e8f2-537e-4f6c-d104768a1214",
}
CHARACTERISTIC_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"
TARGET_DEVICE_NAMES = {"M5_BLE_Mic", "M5_Mic_A"}
RIGHT_ALT_TAP_PACKET = b"M5KRA"
ENTER_TAP_PACKET = b"M5KEN"
KEY_TAP_HOLD_SECONDS = 0.08
RIGHT_ALT_TAP_HOLD_SECONDS = KEY_TAP_HOLD_SECONDS
ENTER_TAP_HOLD_SECONDS = KEY_TAP_HOLD_SECONDS
stream = None
packet_counter = 0
warned_non_audio = False
first_packet_seen = False
last_packet_ts = 0.0
input_sample_rate = 8000
output_sample_rate = 16000
resample_fractional_index = 0.0
resample_last_sample = None
last_read_probe_ts = 0.0
audio_gain = 1.0

# Running DC estimate, updated smoothly across packets; subtracting each packet's
# own mean stepped at every packet boundary and produced an audible buzz.
dc_offset = 0.0

# Bounded jitter buffer between the BLE callback and the sound card. The BLE
# callback must never block: a blocking stream.write() there lets notifications
# pile up invisibly in the WinRT/bleak queue, and playback delay grows to tens of
# seconds. A dedicated writer thread drains this deque; when it holds more than
# max_buffer_seconds of audio the oldest chunks are discarded, so end-to-end
# latency stays bounded regardless of clock drift between the M5 and the PC.
max_buffer_seconds = 0.3
playback_buffer = collections.deque()
playback_buffer_samples = 0
playback_lock = threading.Lock()
dropped_samples_total = 0
last_drop_log_ts = 0.0

# Incoming-rate measurement: detects firmware/input_sample_rate mismatch.
rate_window_start = None
rate_window_bytes = 0

wintypes = ctypes.wintypes
ULONG_PTR = wintypes.WPARAM


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class INPUT_UNION(ctypes.Union):
    _fields_ = (
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    )


class INPUT(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("union", INPUT_UNION),
    )


INPUT_KEYBOARD = 1
VK_RMENU = 0xA5
VK_RETURN = 0x0D
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


def input_structure_size():
    return ctypes.sizeof(INPUT)


def tap_windows_key(vk_code, key_flags=0, hold_seconds=KEY_TAP_HOLD_SECONDS):
    """Send one Windows virtual-key tap."""
    if sys.platform != "win32":
        print("Key tap requested, but this system is not Windows; skipped.")
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    key_down = (INPUT * 1)(
        INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(vk_code, 0, key_flags, 0, 0))),
    )
    key_up = (INPUT * 1)(
        INPUT(INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(vk_code, 0, key_flags | KEYEVENTF_KEYUP, 0, 0))),
    )

    sent = user32.SendInput(len(key_down), key_down, ctypes.sizeof(INPUT))
    if sent != len(key_down):
        raise ctypes.WinError(ctypes.get_last_error())
    time.sleep(hold_seconds)
    sent = user32.SendInput(len(key_up), key_up, ctypes.sizeof(INPUT))
    if sent != len(key_up):
        raise ctypes.WinError(ctypes.get_last_error())


def tap_right_alt(hold_seconds=RIGHT_ALT_TAP_HOLD_SECONDS):
    """Send one Windows Right Alt key tap."""
    tap_windows_key(VK_RMENU, KEYEVENTF_EXTENDEDKEY, hold_seconds)


def tap_enter(hold_seconds=ENTER_TAP_HOLD_SECONDS):
    """Send one Windows Enter key tap."""
    tap_windows_key(VK_RETURN, 0, hold_seconds)


def start_right_alt_tap():
    def worker():
        try:
            tap_right_alt()
        except OSError as e:
            print(f"M5 button trigger failed to send Right Alt: {e}")

    threading.Thread(target=worker, daemon=True).start()


def start_enter_tap():
    def worker():
        try:
            tap_enter()
        except OSError as e:
            print(f"M5 button trigger failed to send Enter: {e}")

    threading.Thread(target=worker, daemon=True).start()

def resample_mono_int16(samples, in_rate=44100, out_rate=16000):
    global resample_fractional_index, resample_last_sample

    if samples.size == 0:
        return samples

    src = samples.astype(np.float32)
    if resample_last_sample is not None:
        src = np.concatenate(([resample_last_sample], src))
    resample_last_sample = float(src[-1])

    if src.size < 2:
        return np.asarray([], dtype=np.int16)

    step = in_rate / float(out_rate)
    positions = []
    pos = resample_fractional_index
    max_pos = src.size - 1
    while pos < max_pos:
        positions.append(pos)
        pos += step

    resample_fractional_index = pos - max_pos

    if not positions:
        return np.asarray([], dtype=np.int16)

    x = np.arange(src.size, dtype=np.float32)
    out = np.interp(np.asarray(positions, dtype=np.float32), x, src)
    return np.clip(out, -32768, 32767).astype(np.int16)

def enqueue_playback(samples):
    """Queue PCM for the writer thread. Never blocks the BLE callback.

    When the buffer exceeds max_buffer_seconds the oldest audio is discarded:
    stale sound is worthless in a live mic link, bounded latency is not.
    """
    global playback_buffer_samples, dropped_samples_total, last_drop_log_ts

    limit = int(max_buffer_seconds * output_sample_rate)
    dropped_now = 0
    with playback_lock:
        playback_buffer.append(samples)
        playback_buffer_samples += samples.size
        while playback_buffer_samples > limit and playback_buffer:
            oldest = playback_buffer.popleft()
            playback_buffer_samples -= oldest.size
            dropped_samples_total += oldest.size
            dropped_now += oldest.size

    if dropped_now:
        now = time.monotonic()
        if now - last_drop_log_ts >= 2.0:
            last_drop_log_ts = now
            print(f"⏭️ 丢弃陈旧音频 {dropped_now * 1000 // output_sample_rate}ms 以保持低延迟 (累计 {dropped_samples_total * 1000 // output_sample_rate}ms)")

def playback_writer_loop():
    """Drains the jitter buffer into the sound card; blocking writes are fine here."""
    global playback_buffer_samples

    while True:
        chunk = None
        with playback_lock:
            if playback_buffer:
                chunk = playback_buffer.popleft()
                playback_buffer_samples -= chunk.size
        if chunk is None:
            time.sleep(0.005)
            continue
        try:
            stream.write(chunk)
        except Exception:
            # Stream closed or device glitch; back off instead of spinning.
            time.sleep(0.05)

def play_local_test_tone(duration=0.25, samplerate=16000, freq=880.0, amp=0.18):
    t = np.arange(int(duration * samplerate), dtype=np.float32) / samplerate
    wave = (np.sin(2.0 * np.pi * freq * t) * (32767 * amp)).astype(np.int16)
    sd.play(wave, samplerate=samplerate, blocking=True)
    print("🔊 本地音频自检完成：如果你刚才没听到短促提示音，问题在系统音频输出而非蓝牙。")

def dump_gatt_map(client):
    print("🧩 当前设备 GATT 映射:")
    for service in client.services:
        print(f"  [Service] {service.uuid}")
        for ch in service.characteristics:
            props = ",".join(ch.properties)
            print(f"    - [Char] {ch.uuid} props=[{props}]")

def choose_notify_characteristic(client):
    target = CHARACTERISTIC_UUID.lower()
    fallback = None
    for service in client.services:
        for ch in service.characteristics:
            uuid = ch.uuid.lower()
            props = {p.lower() for p in ch.properties}
            if uuid == target:
                return ch.uuid
            if "notify" in props and fallback is None:
                fallback = ch.uuid
    return fallback

async def wait_for_services(client, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return client.services
        except BleakError:
            await asyncio.sleep(0.2)
    raise TimeoutError("GATT service discovery timeout")

def is_adv_connectable(adv):
    # bleak version compatibility: some versions expose `connectable`, others don't.
    val = getattr(adv, "connectable", None)
    if isinstance(val, bool):
        return val
    return True

async def connect_with_progress(client, timeout=12.0):
    task = asyncio.create_task(client.connect())
    started = time.monotonic()
    connected_seen = False
    connected_since = None
    while True:
        # Some WinRT stacks report connected state before connect() coroutine returns.
        if client.is_connected and not connected_seen:
            connected_seen = True
            connected_since = time.monotonic()
            print("   ...检测到系统已连接，等待底层初始化完成...")

        # On some Windows/Bleak combinations, connect() may hang despite an established link.
        # If connected state is stable for several seconds, proceed optimistically.
        if connected_seen and connected_since is not None:
            stable_secs = time.monotonic() - connected_since
            if stable_secs >= 6.0:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                print("   ...连接状态已稳定，跳过等待 connect() 返回，继续 GATT 发现...")
                return True

        done, _ = await asyncio.wait({task}, timeout=1.0)
        if task in done:
            # Some bleak versions return None from connect(); authoritative state is client.is_connected.
            task.result()
            return client.is_connected
        elapsed = time.monotonic() - started
        print(f"   ...连接中 {elapsed:.0f}s/{timeout:.0f}s")
        if elapsed >= timeout:
            if client.is_connected:
                print("   ...链路已连，按已连接继续，后续由 GATT 步骤验证...")
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                return True
            task.cancel()
            raise TimeoutError("BLE connect timeout")

async def prepare_windows_ble():
    if sys.platform != "win32":
        return

    # Force console process into MTA; fixes WinRT callback failures in STA threads.
    uninitialize_sta()
    if assert_mta is not None:
        try:
            await assert_mta()
        except BleakError as e:
            msg = str(e)
            if "callbacks are not working" in msg and allow_sta is not None:
                print("⚠️ MTA 检查失败，自动降级为 allow_sta() 继续运行。")
                print("   如果后续扫描仍失败，请关闭占用蓝牙的应用后重试。")
                allow_sta()
            else:
                raise

def notification_handler(sender, data):
    global stream, packet_counter, warned_non_audio, first_packet_seen, last_packet_ts, dc_offset
    if not data:
        return

    packet_counter += 1
    first_packet_seen = True
    last_packet_ts = time.monotonic()
    if packet_counter <= 3:
        print(f"📥 收到通知包: idx={packet_counter}, len={len(data)}, sender={sender}")
    payload = bytes(data)
    if payload == RIGHT_ALT_TAP_PACKET:
        try:
            start_right_alt_tap()
        except OSError as e:
            print(f"M5 button trigger failed to send Right Alt: {e}")
        else:
            print("M5 button trigger: queued Right Alt tap")
        return
    if payload == ENTER_TAP_PACKET:
        try:
            start_enter_tap()
        except OSError as e:
            print(f"M5 button trigger failed to send Enter: {e}")
        else:
            print("M5 button trigger: queued Enter tap")
        return

    # 4-byte payload is typically the counter from mic.ino BLE notify test, not PCM audio.
    if len(data) <= 4:
        if not warned_non_audio:
            print("⚠️ 收到的是短包通知（例如 4 字节计数器），当前固件不是音频推流模式。")
            print("   这是 BLE 联通测试成功，但不会产生可播放的麦克风声音。")
            warned_non_audio = True

        # Convert counter notifications into a short synthetic tone so output path can be verified.
        if stream is not None:
            counter = int.from_bytes(data.ljust(4, b"\x00"), byteorder="little", signed=False)
            freq = 350.0 + float(counter % 10) * 40.0
            tone_len = 160
            t = np.arange(tone_len, dtype=np.float32) / output_sample_rate
            synth = (np.sin(2.0 * np.pi * freq * t) * 7000.0).astype(np.int16)
            enqueue_playback(synth)

        if packet_counter <= 10 or packet_counter % 50 == 0:
            print(f"📶 通知持续中（测试模式）: packets={packet_counter}, last_len={len(data)}")
        return

    if stream is None:
        if packet_counter <= 10 or packet_counter % 50 == 0:
            print(f"📶 已收到音频包，但音频输出流未就绪: packets={packet_counter}, len={len(data)}")
        return

    # Ensure int16 alignment for PCM parsing.
    if len(data) % 2 != 0:
        return
    
    # Parse mono PCM from firmware and apply light conditioning before resampling.
    mono_data = np.frombuffer(data, dtype=np.int16).astype(np.float32)

    # Update the running DC estimate slowly so packet boundaries stay continuous.
    dc_offset += 0.05 * (float(np.mean(mono_data)) - dc_offset)
    mono_data = mono_data - dc_offset
    mono_data = np.clip(mono_data * audio_gain, -32768.0, 32767.0).astype(np.int16)

    mono_data = resample_mono_int16(mono_data, in_rate=input_sample_rate, out_rate=output_sample_rate)
    if mono_data.size == 0:
        return

    if packet_counter <= 10:
        max_volume = np.max(np.abs(mono_data))
        print(f"📡 蓝牙音频流传输中... 16k重采样后实时音量振幅: {max_volume}")

    # 进入有界抖动缓冲，由独立写线程送入声卡；回调本身绝不阻塞
    enqueue_playback(mono_data)

    # Measure the real incoming sample rate over 5 s windows; a mismatch with
    # input_sample_rate means wrong pitch/speed and a growing or starving buffer.
    global rate_window_start, rate_window_bytes
    now = time.monotonic()
    if rate_window_start is None:
        rate_window_start = now
    rate_window_bytes += len(data)
    window = now - rate_window_start
    if window >= 5.0:
        measured_rate = (rate_window_bytes / 2.0) / window
        with playback_lock:
            buffered_ms = playback_buffer_samples * 1000.0 / output_sample_rate
        dropped_ms = dropped_samples_total * 1000.0 / output_sample_rate
        print(f"📊 输入采样率实测 ~{measured_rate:.0f} Hz (脚本假定 {input_sample_rate}), 播放缓冲 {buffered_ms:.0f}ms, 累计丢弃 {dropped_ms:.0f}ms")
        if measured_rate > input_sample_rate * 1.1 or measured_rate < input_sample_rate * 0.9:
            print(f"   ⚠️ 实测速率与假定不符：请把 input_sample_rate 改为 {measured_rate:.0f} 左右，音调/速度才正确。")
        rate_window_start = now
        rate_window_bytes = 0

async def find_target_device(timeout=6.0):
    print(f"🔎 扫描附近 BLE 设备 {timeout:.0f}s，检查目标是否真的在发 BLE 广播...")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)

    service_match = None
    name_match = None
    target_services = {x.lower() for x in TARGET_SERVICE_UUIDS}

    for _, (dev, adv) in found.items():
        name = dev.name or adv.local_name or "<unknown>"
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        connectable = is_adv_connectable(adv)

        if any(x in name.lower() for x in ("m5", "mic")):
            print(f"  - 候选设备: name={name}, addr={dev.address}, connectable={connectable}, service_uuids={uuids}")

        if any(u in target_services for u in uuids) and connectable and service_match is None:
            service_match = dev

        if name in TARGET_DEVICE_NAMES and connectable and name_match is None:
            name_match = dev

    if service_match is not None:
        print("✅ 找到带目标 Service UUID 的设备，将按服务优先连接。")
        return service_match

    if name_match is not None:
        print("⚠️ 找到同名设备，但广播里没有目标 Service UUID。")
        print("   当前脚本接受的服务 UUID:")
        for svc in TARGET_SERVICE_UUIDS:
            print(f"   - {svc}")
        print("   这通常是固件 UUID 不一致，或该设备只在经典蓝牙模式下可见。")
        return name_match

    print("❌ 未找到目标 BLE 广播。请确认 M5 正在广播且未被手机占用连接。")
    return None

async def main():
    global stream, first_packet_seen, packet_counter, last_packet_ts, last_read_probe_ts
    
    print("🔍 [1/2] 绕过声卡独占，开始利用 Service UUID 直接轰炸 M5...")
    await prepare_windows_ble()
    
    device = None
    while device is None:
        try:
            device = await find_target_device(timeout=5.0)
            if not device:
                print("⏳ 正在全力捕捉老频段信号... (请断开手机连接，让 M5 保持 Ready)")
        except Exception as e:
            print(f"⚠️ 扫描异常: {e}")
            await asyncio.sleep(1)

    print(f"🔗 [2/2] 锁定设备成功！正在强行突入建立 BLE 握手...")
    
    # Retry strategies are needed on some Windows adapters where BLEDevice handle is stale.
    connect_candidates = [
        ("scan返回的 BLEDevice", device),
        ("设备地址字符串", device.address),
    ]

    refreshed = await BleakScanner.find_device_by_address(device.address, timeout=4.0)
    if refreshed is not None:
        connect_candidates.append(("按地址刷新后的 BLEDevice", refreshed))

    client = None
    try:
        connected = False
        timeout_failures = 0
        not_found_failures = 0
        winrt_profiles = [
            ("public+禁缓存", {"address_type": "public", "use_cached_services": False}, False, 25.0),
            ("public+禁缓存+配对", {"address_type": "public", "use_cached_services": False}, True, 25.0),
            ("random+禁缓存", {"address_type": "random", "use_cached_services": False}, False, 25.0),
            ("random+禁缓存+配对", {"address_type": "random", "use_cached_services": False}, True, 30.0),
        ]

        for idx, (label, target) in enumerate(connect_candidates, start=1):
            for pidx, (pname, winrt_opts, pair_mode, step_timeout) in enumerate(winrt_profiles, start=1):
                print(f"⏱️ 步骤A.{idx}.{pidx}: 使用{label} + {pname} 建立 BLE 连接 (最多 {int(step_timeout)}s)...")
                if pair_mode:
                    print("   如出现系统配对弹窗，请点允许/配对。")

                client = BleakClient(target, timeout=10.0, winrt=winrt_opts, pair=pair_mode)
                try:
                    connected = await connect_with_progress(client, timeout=step_timeout)
                    if client.is_connected:
                        connected = True
                        print(f"✅ 步骤A.{idx}.{pidx}完成: BLE 已连接")
                        break
                except Exception as e:
                    print(f"⚠️ 步骤A.{idx}.{pidx}失败: {e}")
                    err = str(e).lower()
                    if "timeout" in err:
                        timeout_failures += 1
                    if "was not found" in err:
                        not_found_failures += 1
                    try:
                        if client.is_connected:
                            await client.disconnect()
                    except Exception:
                        pass

            if connected and client is not None and client.is_connected:
                break

        if client is None or (not connected) or (not client.is_connected):
            print("❌ BLE 握手失败：多策略连接均未成功。")
            print("   建议先在 Windows 蓝牙设置里删除该设备后重启蓝牙再试。")
            if timeout_failures > 0 and not_found_failures > 0:
                print("   诊断结论: 设备地址类型更像 public，但系统层连接建立被阻塞。")
                print("   建议动作: 先手动配对 M5_Mic_A，再重新运行本脚本。")
            elif timeout_failures > 0:
                print("   诊断结论: 设备可发现但连接握手超时，常见于系统蓝牙栈卡住。")
            elif not_found_failures > 0:
                print("   诊断结论: 设备广播不稳定或地址类型不匹配。")
            return

        print("⏱️ 步骤B: 正在发现 GATT 服务 (最多 10s)...")
        await wait_for_services(client, timeout=10.0)
        print("✅ 步骤B完成: 已获取 GATT")
        dump_gatt_map(client)

        selected_char = choose_notify_characteristic(client)
        if selected_char is None:
            print("❌ 已连接到设备，但没有任何支持 notify 的特征。")
            print("   该设备大概率不是当前这份 BLE 麦克风固件。")
            return

        if selected_char.lower() != CHARACTERISTIC_UUID.lower():
            print("⚠️ 目标特征 UUID 不存在，已自动切换到首个 notify 特征。")
            print(f"   期望特征: {CHARACTERISTIC_UUID}")
            print(f"   实际特征: {selected_char}")
            print("   建议把 Python 的 CHARACTERISTIC_UUID 改成上述实际值。")

        print("⏱️ 步骤C: 正在订阅 notify (最多 8s)...")
        await asyncio.wait_for(client.start_notify(selected_char, notification_handler), timeout=8.0)
        print("✅ 步骤C完成: notify 已订阅")
        print("\n🎉 [蓝牙层已通] M5 屏幕应该已经亮起 Connected 了！")

        # Wait briefly to verify notifications are actually flowing.
        await asyncio.sleep(2.0)
        if not first_packet_seen:
            print("⚠️ 已订阅 notify 但 2 秒内未收到任何数据包。")
            print("   可能原因: 固件未调用 notify、订阅了错误特征、或设备侧发送被限流。")
            print("   若此处频繁出现，请优先检查 mic.ino 的 loop() 是否持续 notify。")
        
        # 蓝牙通了之后，再在内部安全地初始化 1 通道声卡
        print("🎵 正在挂载 Windows 1通道虚拟音频管道...")
        device_index = None
        for idx, dev in enumerate(sd.query_devices()):
            if "CABLE Input" in dev["name"] and dev["max_output_channels"] > 0:
                device_index = idx
                break

        if device_index is not None:
            stream = sd.OutputStream(samplerate=16000, channels=1, dtype='int16', device=device_index, latency='low')
            stream.start()
            print("🚀 [SUCCESS] 1通道音频管道挂载成功！全部链路打通，请开始说话...")
        else:
            print("⚠️ 未找到 VB-Cable，自动回退到系统默认扬声器输出。")
            stream = sd.OutputStream(samplerate=16000, channels=1, dtype='int16', latency='low')
            stream.start()
            print("🚀 [SUCCESS] 已使用默认扬声器输出。")

        threading.Thread(target=playback_writer_loop, daemon=True).start()

        try:
            play_local_test_tone()
        except Exception as e:
            print(f"⚠️ 本地音频自检失败: {e}")

        no_packet_wait_secs = 0
        notify_resubscribe_count = 0
        stalled_resubscribe_count = 0
        last_stall_recover_ts = 0.0
        stall_warning_latched = False
        while True:
            if first_packet_seen:
                since_last = time.monotonic() - last_packet_ts
                if since_last > 5.0 and not stall_warning_latched:
                    stall_warning_latched = True
                    print("⚠️ 超过 5 秒未收到新通知包，设备可能暂停推流或已掉线。")
                if since_last <= 2.0:
                    stall_warning_latched = False

                if since_last >= 10.0 and (time.monotonic() - last_stall_recover_ts) >= 8.0:
                    last_stall_recover_ts = time.monotonic()
                    try:
                        await client.stop_notify(selected_char)
                        await asyncio.sleep(0.2)
                        await client.start_notify(selected_char, notification_handler)
                        stalled_resubscribe_count += 1
                        print(f"🔁 检测到断流，已重置 notify (断流恢复第 {stalled_resubscribe_count} 次)")
                    except Exception as e:
                        print(f"⚠️ 断流重置 notify 失败: {e}")

                    try:
                        polled = await client.read_gatt_char(selected_char)
                        if polled:
                            print(f"📤 断流恢复读取到特征值 len={len(polled)}")
                            notification_handler(selected_char, polled)
                        else:
                            print("📭 断流恢复读取为空")
                    except Exception as e:
                        print(f"⚠️ 断流恢复主动读取失败: {e}")

            if not first_packet_seen:
                no_packet_wait_secs += 1
                if no_packet_wait_secs % 5 == 0:
                    print(f"⏳ 等待 BLE 数据包中... ({no_packet_wait_secs}s, 已连接但尚未收到通知)")
                if no_packet_wait_secs == 10:
                    print("💡 10秒无通知：请确认 M5 当前固件在 loop() 中持续调用 notify。")
                if no_packet_wait_secs in (20, 40) and notify_resubscribe_count < 2:
                    try:
                        await client.stop_notify(selected_char)
                        await asyncio.sleep(0.2)
                        await client.start_notify(selected_char, notification_handler)
                        notify_resubscribe_count += 1
                        print(f"🔁 已重置 notify 订阅 (第 {notify_resubscribe_count} 次)")
                    except Exception as e:
                        print(f"⚠️ 重置 notify 失败: {e}")
                if no_packet_wait_secs == 30:
                    print("💡 30秒无通知：Python 侧已连通；优先怀疑设备端未发送或 notify 未真正开启。")

                now = time.monotonic()
                if now - last_read_probe_ts >= 10.0:
                    last_read_probe_ts = now
                    try:
                        polled = await client.read_gatt_char(selected_char)
                        if polled:
                            print(f"📤 主动读取到特征值 len={len(polled)}，将按音频包路径处理。")
                            notification_handler(selected_char, polled)
                        else:
                            print("📭 主动读取返回空值。")
                    except Exception as e:
                        print(f"⚠️ 主动读取特征值失败: {e}")
            await asyncio.sleep(1)

    except TimeoutError as e:
        print(f"\n❌ 步骤超时: {e}")
        print("   可能有系统蓝牙配对弹窗被遮挡，请打开 Windows 设置确认。")
    except Exception as e:
        print(f"\n❌ 连接或挂载失败: {e}")
    finally:
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if stream is not None:
            stream.stop()
            stream.close()
        print("\n[INFO] 管道安全关闭。")
