#include <M5StickCPlus.h>
#include <driver/i2s.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// 精准对齐你的新 Python 脚本中的双组合 UUID
#define SERVICE_UUID           "4fafc201-1fb5-459e-8fcc-c5c9c331914b"
#define CHARACTERISTIC_UUID    "beb5483e-36e1-4688-b7f5-ea07361b26a8"

#define PIN_CLK     0
#define PIN_DATA    34

// Keep the BLE payload aligned with the Python client (128 int16 samples = 256 bytes).
#define READ_LEN    (2 * 128)
#define MIC_GAIN    3

uint8_t BUFFER[READ_LEN] = {0};
int16_t *micSamples = nullptr;
uint32_t packetCounter = 0;
uint32_t zeroPacketCounter = 0;

BLECharacteristic *pCharacteristic;
bool deviceConnected = false;

// 蓝牙连接状态回调
class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) { 
        deviceConnected = true; 
        M5.Lcd.fillScreen(BLACK);
        M5.Lcd.setCursor(0, 10);
        M5.Lcd.println("BLE Connected!");
    };
    void onDisconnect(BLEServer* pServer) {
        deviceConnected = false;
        M5.Lcd.fillScreen(BLACK);
        M5.Lcd.setCursor(0, 10);
        M5.Lcd.println("BLE Disconnected\nAdvertising...");
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
    size_t bytesread = 0;
    while (1) {
        esp_err_t ret = i2s_read(I2S_NUM_0, (char *)BUFFER, READ_LEN, &bytesread, portMAX_DELAY);
        if (ret == ESP_OK && bytesread > 0) {
            micSamples = (int16_t *)BUFFER;
            drawWaveform();

            // Diagnostic for the current problem: show whether the mic path is alive or stuck at silence.
            int16_t *samples = (int16_t *)BUFFER;
            int sampleCount = bytesread / 2;
            int nonZeroCount = 0;
            int32_t peak = 0;
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
            }

            // Keep BLE payload compatible with the Python side.
            if (deviceConnected) {
                pCharacteristic->setValue(BUFFER, bytesread);
                pCharacteristic->notify();
            }

            packetCounter++;
        }

        if (zeroPacketCounter == 50) {
            M5.Lcd.setCursor(0, 90);
            M5.Lcd.fillRect(0, 90, 160, 12, BLACK);
            M5.Lcd.print("Mic silence/check wiring");
        }

        vTaskDelay(5 / portTICK_PERIOD_MS);
    }
}

// Official example-style I2S parameters; this has proven more reliable for the M5StickC Plus mic path.
void i2sInit() {
    i2s_config_t i2s_config = {
        .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM),
        .sample_rate = 44100,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ALL_RIGHT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count    = 2,
        .dma_buf_len      = 128,
    };

    i2s_pin_config_t pin_config;
#if (ESP_IDF_VERSION > ESP_IDF_VERSION_VAL(4, 3, 0))
    pin_config.mck_io_num = I2S_PIN_NO_CHANGE;
#endif
    pin_config.bck_io_num   = I2S_PIN_NO_CHANGE;
    pin_config.ws_io_num    = PIN_CLK;
    pin_config.data_out_num = I2S_PIN_NO_CHANGE;
    pin_config.data_in_num  = PIN_DATA;

    i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
    i2s_set_pin(I2S_NUM_0, &pin_config);
    i2s_set_clk(I2S_NUM_0, 44100, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
}

void setup() {
    M5.begin();
    M5.Lcd.setRotation(3);
    M5.Lcd.fillScreen(BLACK);
    M5.Lcd.setCursor(0, 10);
    M5.Lcd.println("AI MIC Booting...");

    // 1. 初始化麦克风物理硬件
    i2sInit();

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

    xTaskCreatePinnedToCore(mic_record_task, "mic_record_task", 4096, NULL, 1, NULL, 0);
}

void loop() {
    M5.update(); // 保持核心按键和电源管理状态刷新
    delay(100);
}