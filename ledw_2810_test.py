from rpi_ws281x import PixelStrip, Color
import time

LED_COUNT = 1        # Number of LEDs in your test setup
LED_PIN = 10         # GPIO10 (SPI0 MOSI)
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_BRIGHTNESS = 64  # 0–255
LED_CHANNEL = 0
LED_INVERT = False

strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
                   LED_INVERT, LED_BRIGHTNESS, LED_CHANNEL)
                   
strip.begin()

def solid(color, t=1.0):
    for i in range(LED_COUNT):
        strip.setPixelColor(i, color)
    strip.show()
    time.sleep(t)

# Test sequence
solid(Color(255, 0, 0), 1.0)   # Red
solid(Color(0, 255, 0), 1.0)   # Green
solid(Color(0, 0, 255), 1.0)   # Blue
solid(Color(0, 0, 0), 1.0)     # Off