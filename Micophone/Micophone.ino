#include <M5Unified.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// 精准对齐你的新 Python 脚本中的双组合 UUID
#define SERVICE_UUID           "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID    "beb5483e-36e1-4688-b7f5-ea07361b26a8"

#define MIC_SAMPLE_RATE 8000
#define READ_SAMPLES 120
#define READ_LEN    (READ_SAMPLES * sizeof(int16_t))
#define MIC_GAIN    3

int16_t BUFFER[READ_SAMPLES] = {0};
int16_t *micSamples = nullptr;
uint32_t packetCounter = 0;
uint32_t zeroPacketCounter = 0;
volatile bool uiConnectedChanged = false;
volatile bool uiIsConnected = false;

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
        BLEDevice::startAdvertising(); // 断开后自动重新广播
    }
};

void drawWaveform() {
    if (micSamples == nullptr) {
        return;
    }

    static uint16_t oldy[160] = {0};
    for (int n = 0; n < 128 && n < 160; n++) {
        int y = micSamples[n] * MIC_GAIN;
        y = map(y, INT16_MIN, INT16_MAX, 10, 70);
        M5.Lcd.drawPixel(n, oldy[n], WHITE);
        M5.Lcd.drawPixel(n, y, BLACK);
        oldy[n] = y;
    }
}

// Use the official example's style of isolated mic task so audio capture is not blocked by BLE work.
void mic_record_task(void *arg) {
    while (1) {
        if (M5.Mic.record(BUFFER, READ_SAMPLES, MIC_SAMPLE_RATE)) {
            size_t bytesread = READ_LEN;
            micSamples = (int16_t *)BUFFER;
            drawWaveform();

            // Diagnostic for the current problem: show whether the mic path is alive or stuck at silence.
            int16_t *samples = (int16_t *)BUFFER;
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
                M5.Lcd.setCursor(0, 80);
                M5.Lcd.fillRect(0, 80, 160, 20, BLACK);
                M5.Lcd.printf("pkt:%lu nz:%d pk:%ld", (unsigned long)packetCounter, nonZeroCount, (long)peak);
                Serial.printf("pkt:%lu nz:%d pk:%ld first:%d bytes:%u\n", (unsigned long)packetCounter, nonZeroCount, (long)peak, (int)firstSample, (unsigned int)bytesread);
            }

            // Keep BLE payload conservative (80 int16 = 160 bytes) to improve stability across adapters/MTU.
            if (deviceConnected) {
                pCharacteristic->setValue((uint8_t *)BUFFER, bytesread);
                pCharacteristic->notify();

                if (packetCounter < 5 || packetCounter % 100 == 0) {
                    Serial.printf("notify sent: len=%u\n", (unsigned int)bytesread);
                }
            }

            packetCounter++;
        }

        if (zeroPacketCounter == 50) {
            M5.Lcd.setCursor(0, 90);
            M5.Lcd.fillRect(0, 90, 160, 12, BLACK);
            M5.Lcd.print("Mic silence/check wiring");
        }

        // Keep loop responsive without inserting large gaps between audio chunks.
        vTaskDelay(1 / portTICK_PERIOD_MS);
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

    M5.Lcd.println("BLE Ready!\nSearch: M5_BLE_Mic");

    if (micOk) {
        xTaskCreatePinnedToCore(mic_record_task, "mic_record_task", 8192, NULL, 1, NULL, 1);
    }
}

void loop() {
    M5.update(); // 保持核心按键和电源管理状态刷新
    if (uiConnectedChanged) {
        uiConnectedChanged = false;
        M5.Lcd.setCursor(0, 10);
        M5.Lcd.fillRect(0, 10, 160, 20, BLACK);
        if (uiIsConnected) {
            M5.Lcd.println("BLE Connected!");
        } else {
            M5.Lcd.println("BLE Disconnected");
        }
    }
    delay(100);
}