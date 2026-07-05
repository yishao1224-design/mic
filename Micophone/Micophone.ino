#include <M5Unified.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// 精准对齐你的新 Python 脚本中的双组合 UUID
#define SERVICE_UUID           "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID    "beb5483e-36e1-4688-b7f5-ea07361b26a8"

#define MIC_SAMPLE_RATE 8000
#define READ_SAMPLES 240  // 30 ms per packet; larger packets halve BLE notify overhead
#define READ_LEN    (READ_SAMPLES * sizeof(int16_t))
#define MIC_GAIN    3

int16_t BUFFER[2][READ_SAMPLES] = {0};
int16_t *micSamples = nullptr;
uint32_t packetCounter = 0;
uint32_t zeroPacketCounter = 0;
volatile bool uiConnectedChanged = false;
volatile bool uiIsConnected = false;
volatile bool rightAltTapPending = false;
volatile bool enterTapPending = false;
static const uint8_t RIGHT_ALT_TAP_PACKET[] = {'M', '5', 'K', 'R', 'A'};
static const uint8_t ENTER_TAP_PACKET[] = {'M', '5', 'K', 'E', 'N'};

BLECharacteristic *pCharacteristic;
bool deviceConnected = false;

// 蓝牙连接状态回调
class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) { 
        deviceConnected = true; 
        uiIsConnected = true;
        uiConnectedChanged = true;
    };
    void onDisconnect(BLEServer* pServer) {
        deviceConnected = false;
        uiIsConnected = false;
        uiConnectedChanged = true;
        rightAltTapPending = false;
        enterTapPending = false;
        BLEDevice::startAdvertising(); // 断开后自动重新广播
    }
};

// Screen layout (240x135 in rotation 3): two 10px text rows on top, waveform below.
#define STATUS_ROW_Y   1
#define DIAG_ROW_Y     11
#define WAVE_TOP       22

bool micTaskRunning = false;

void drawStatusBar() {
    M5.Lcd.fillRect(0, 0, M5.Lcd.width(), 10, BLACK);
    M5.Lcd.setCursor(0, STATUS_ROW_Y);
    if (uiIsConnected) {
        M5.Lcd.setTextColor(GREEN, BLACK);
        M5.Lcd.print("BLE Connected");
    } else {
        M5.Lcd.setTextColor(YELLOW, BLACK);
        M5.Lcd.print("Advertising: M5_BLE_Mic");
    }
    M5.Lcd.setTextColor(WHITE, BLACK);
}

// Full-screen oscilloscope trace: erase the previous polyline in the background
// color (the old code erased with WHITE on a BLACK screen, littering the display),
// redraw the center reference line, then draw the new polyline.
void drawWaveform() {
    if (micSamples == nullptr) {
        return;
    }

    const int w = M5.Lcd.width();
    const int bottom = M5.Lcd.height() - 1;
    const int mid = (WAVE_TOP + bottom) / 2;
    const int halfH = (bottom - WAVE_TOP) / 2;

    static int16_t oldy[320];
    static bool hasOld = false;

    if (hasOld) {
        for (int x = 1; x < w; x++) {
            M5.Lcd.drawLine(x - 1, oldy[x - 1], x, oldy[x], BLACK);
        }
    }
    M5.Lcd.drawFastHLine(0, mid, w, DARKGREY);

    for (int x = 0; x < w; x++) {
        int32_t v = (int32_t)micSamples[(x * READ_SAMPLES) / w] * MIC_GAIN;
        if (v > 32767) v = 32767;
        if (v < -32768) v = -32768;
        oldy[x] = mid - (int)((v * halfH) / 32768);
    }
    for (int x = 1; x < w; x++) {
        M5.Lcd.drawLine(x - 1, oldy[x - 1], x, oldy[x], GREEN);
    }
    hasOld = true;
}

// Double-buffered capture: M5.Mic.record() is asynchronous (DMA fills the buffer in
// the background), so one buffer records while the previous, completed one is streamed.
// Streaming BUFFER right after record() returned was sending half-filled data.
void sendControlNotifications() {
    if (!deviceConnected) {
        rightAltTapPending = false;
        enterTapPending = false;
        return;
    }

    if (rightAltTapPending) {
        rightAltTapPending = false;
        pCharacteristic->setValue((uint8_t *)RIGHT_ALT_TAP_PACKET, sizeof(RIGHT_ALT_TAP_PACKET));
        pCharacteristic->notify();
        Serial.println("button click: Right Alt tap marker sent");
        vTaskDelay(5 / portTICK_PERIOD_MS);
    }
    if (enterTapPending) {
        enterTapPending = false;
        pCharacteristic->setValue((uint8_t *)ENTER_TAP_PACKET, sizeof(ENTER_TAP_PACKET));
        pCharacteristic->notify();
        Serial.println("button hold: Enter tap marker sent");
        vTaskDelay(5 / portTICK_PERIOD_MS);
    }
}


void mic_record_task(void *arg) {
    int recIdx = 0;
    while (!M5.Mic.record(BUFFER[recIdx], READ_SAMPLES, MIC_SAMPLE_RATE)) {
        sendControlNotifications();
        vTaskDelay(10 / portTICK_PERIOD_MS);
    }

    while (1) {
        // All LCD drawing happens in this task so nothing races on the SPI bus.
        if (uiConnectedChanged) {
            uiConnectedChanged = false;
            drawStatusBar();
        }
        sendControlNotifications();

        if (!M5.Mic.record(BUFFER[recIdx ^ 1], READ_SAMPLES, MIC_SAMPLE_RATE)) {
            vTaskDelay(10 / portTICK_PERIOD_MS);
            continue;
        }

        while (M5.Mic.isRecording() >= 2) {
            sendControlNotifications();
            vTaskDelay(1 / portTICK_PERIOD_MS);
        }

        int16_t *samples = BUFFER[recIdx];
        size_t bytesread = READ_LEN;
        micSamples = samples;
        if ((packetCounter & 3) == 0) {
            drawWaveform();
        }

        int sampleCount = bytesread / 2;
        int nonZeroCount = 0;
        int32_t peak = 0;
        int16_t firstSample = samples[0];
        for (int i = 0; i < sampleCount; i++) {
            int32_t v = abs(samples[i]);
            if (v != 0) {
                nonZeroCount++;
            }
            if (v > peak) {
                peak = v;
            }
        }

        if (peak == 0) {
            zeroPacketCounter++;
        } else {
            zeroPacketCounter = 0;
        }

        if (packetCounter < 5 || packetCounter % 50 == 0) {
            M5.Lcd.fillRect(0, DIAG_ROW_Y - 1, M5.Lcd.width(), 10, BLACK);
            M5.Lcd.setCursor(0, DIAG_ROW_Y);
            M5.Lcd.printf("pkt:%lu nz:%d pk:%ld", (unsigned long)packetCounter, nonZeroCount, (long)peak);
            if (zeroPacketCounter >= 50) {
                M5.Lcd.setTextColor(RED, BLACK);
                M5.Lcd.print("  MIC SILENT");
                M5.Lcd.setTextColor(WHITE, BLACK);
            }
            Serial.printf("pkt:%lu nz:%d pk:%ld first:%d bytes:%u\n", (unsigned long)packetCounter, nonZeroCount, (long)peak, (int)firstSample, (unsigned int)bytesread);
        }

        if (deviceConnected) {
            pCharacteristic->setValue((uint8_t *)samples, bytesread);
            pCharacteristic->notify();

            if (packetCounter < 5 || packetCounter % 100 == 0) {
                Serial.printf("notify sent: len=%u\n", (unsigned int)bytesread);
            }
        } else {
            rightAltTapPending = false;
            enterTapPending = false;
        }

        packetCounter++;
        recIdx ^= 1;
    }
}


void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    Serial.begin(115200);
    M5.Lcd.setRotation(3);
    M5.Lcd.fillScreen(BLACK);
    M5.Lcd.setCursor(0, 10);
    M5.Lcd.println("AI MIC Booting...");

    M5.Speaker.end();
    bool micOk = M5.Mic.begin();
    M5.Lcd.setCursor(0, 20);
    M5.Lcd.fillRect(0, 20, 160, 18, BLACK);
    M5.Lcd.print(micOk ? "mic:init ok" : "mic:init fail");
    if (!micOk) {
        Serial.println("Mic init failed, skip capture task to avoid reboot loop.");
    }

    // 2. 乐鑫官方标准低功耗蓝牙初始化
    BLEDevice::init("M5_BLE_Mic"); // 名字对齐你的 TARGET_DEVICE_NAMES
    BLEDevice::setMTU(517); // 允许 480 字节音频负载放进单个 notify，避免被截断
    BLEServer *pServer = BLEDevice::createServer();
    pServer->setCallbacks(new MyServerCallbacks());

    BLEService *pService = pServer->createService(SERVICE_UUID);
    pCharacteristic = pService->createCharacteristic(
                        CHARACTERISTIC_UUID,
                        BLECharacteristic::PROPERTY_READ   |
                        BLECharacteristic::PROPERTY_NOTIFY 
                      );
    pCharacteristic->addDescriptor(new BLE2902());
    pService->start();

    // 配置原生安全广告包
    BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(false); 
    pAdvertising->setMinPreferred(0x06);  
    pAdvertising->setMinPreferred(0x12);
    
    BLEDevice::startAdvertising();

    // Switch from boot messages to the runtime layout: status bar + full-screen waveform.
    M5.Lcd.fillScreen(BLACK);
    drawStatusBar();

    if (micOk) {
        micTaskRunning = true;
        xTaskCreatePinnedToCore(mic_record_task, "mic_record_task", 8192, NULL, 1, NULL, 1);
    } else {
        M5.Lcd.setCursor(0, DIAG_ROW_Y);
        M5.Lcd.setTextColor(RED, BLACK);
        M5.Lcd.print("mic:init fail");
        M5.Lcd.setTextColor(WHITE, BLACK);
    }
}

void loop() {
    M5.update(); // 保持核心按键和电源管理状态刷新
    if (M5.BtnA.wasHold()) {
        enterTapPending = deviceConnected;
        Serial.println(deviceConnected ? "button hold: Enter tap queued" : "button hold ignored: BLE disconnected");
    } else if (M5.BtnA.wasClicked()) {
        rightAltTapPending = deviceConnected;
        Serial.println(deviceConnected ? "button click: Right Alt tap queued" : "button click ignored: BLE disconnected");
    }
    // The mic task owns the LCD; only draw from here if it never started.
    if (uiConnectedChanged && !micTaskRunning) {
        uiConnectedChanged = false;
        drawStatusBar();
    }
    delay(10);
}
