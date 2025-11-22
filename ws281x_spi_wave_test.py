#!/usr/bin/env python3
"""
Minimal WS281x test over SPI (GPIO10 / SPI0 MOSI)

- Assumes 1 WS2811/WS2812 LED connected via level shifter to GPIO10.
- Uses SPI at 2.4 MHz and encodes each data bit into 3 SPI bits:
    WS '1' -> 110
    WS '0' -> 100
- Runs a smooth green "breathing" wave to test flicker/stability.
"""

import spidev
import time
import math

# ----- Config -----
LED_COUNT = 1
SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED_HZ = 2400000  # 2.4 MHz (3 SPI bits per WS bit -> ~1.25µs)

RESET_US = 80  # latch/reset time in microseconds


# ----- Encoding -----
# WS bit timings:
#  - '1': high ~0.8µs, low ~0.45µs
#  - '0': high ~0.4µs, low ~0.85µs
# We approximate with SPI bits @ 2.4MHz => 0.416µs/bit:
#  - '1' -> 110 (≈0.83µs high, 0.416µs low)
#  - '0' -> 100 (≈0.416µs high, 0.833µs low)

BIT_1 = 0b110
BIT_0 = 0b100

def encode_byte(byte: int) -> list[int]:
    """Encode one 8-bit color byte into 8*3 = 24 SPI bits (packed into 3 bytes)."""
    out_bits = 0
    for i in range(8):
        bit = (byte & (1 << (7 - i))) != 0
        out_bits = (out_bits << 3) | (BIT_1 if bit else BIT_0)

    # out_bits is 24 bits; pack into 3 bytes MSB first
    return [
        (out_bits >> 16) & 0xFF,
        (out_bits >> 8) & 0xFF,
        out_bits & 0xFF,
    ]

def encode_rgb(r: int, g: int, b: int) -> list[int]:
    """WS281x expects GRB order."""
    buf: list[int] = []
    for byte in (g, r, b):
        buf.extend(encode_byte(byte))
    return buf


# ----- SPI driver -----

spi = spidev.SpiDev()
spi.open(SPI_BUS, SPI_DEVICE)
spi.max_speed_hz = SPI_SPEED_HZ
spi.mode = 0

def show_color(r: int, g: int, b: int):
    r = max(0, min(255, r))
    g = max(0, min(255, g))
    b = max(0, min(255, b))

    buf = encode_rgb(r, g, b) * LED_COUNT
    spi.xfer2(buf)
    # reset/latch
    time.sleep(RESET_US / 1_000_000.0)


def off():
    show_color(0, 0, 0)


# ----- Wave test -----

def wave_test():
    print("SPI WS281x wave test on GPIO10; Ctrl+C to stop")
    start = time.monotonic()
    try:
        while True:
            t = time.monotonic() - start
            # breathing brightness 0..1
            f = 0.5 - 0.5 * math.cos(2.0 * math.pi * t / 2.0)  # 2s period
            g = int(255 * f)
            show_color(0, g, 0)
            time.sleep(1.0 / 60.0)  # 60 FPS
    except KeyboardInterrupt:
        pass
    finally:
        off()
        spi.close()
        print("Stopped, LED off")


if __name__ == "__main__":
    wave_test()

