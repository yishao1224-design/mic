#include <M5Unified.h>

#define MIC_SAMPLE_RATE 8000
#define READ_SAMPLES 120
#define READ_LEN    (READ_SAMPLES * sizeof(int16_t))
#define GAIN_FACTOR 3
int16_t BUFFER[READ_SAMPLES] = {0};

uint16_t oldy[160];
int16_t *adcBuffer = NULL;

void showSignal() {
    int y;
    for (int n = 0; n < 160; n++) {
        y = adcBuffer[n] * GAIN_FACTOR;
        y = map(y, INT16_MIN, INT16_MAX, 10, 70);
        M5.Lcd.drawPixel(n, oldy[n], WHITE);
        M5.Lcd.drawPixel(n, y, BLACK);
        oldy[n] = y;
    }
}

void mic_record_task(void *arg) {
    while (1) {
        if (M5.Mic.record(BUFFER, READ_SAMPLES, MIC_SAMPLE_RATE)) {
            adcBuffer = BUFFER;
            showSignal();
        }
        vTaskDelay(1 / portTICK_PERIOD_MS);
    }
}

void setup() {
    auto cfg = M5.config();
    M5.begin(cfg);
    M5.Lcd.setRotation(3);
    M5.Lcd.fillScreen(WHITE);
    M5.Lcd.setTextColor(BLACK, WHITE);
    M5.Lcd.println("mic test");

    M5.Speaker.end();
    M5.Mic.begin();
    xTaskCreate(mic_record_task, "mic_record_task", 2048, NULL, 1, NULL);
}

void loop() {
    printf("loop cycling\n");
    vTaskDelay(1000 / portTICK_PERIOD_MS);  // otherwise the main task wastes half
                                          // of the cpu cycles
}
