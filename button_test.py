#!/usr/bin/env python3
"""
Simple GPIO input test for Hearo project
Detects button presses on GPIO17 using libgpiod.
"""

import gpiod
import time

CHIP = "gpiochip0"   # Default GPIO chip on Armbian/RPi
PIN = 17             # BCM numbering
DEBOUNCE_TIME = 0.3  # seconds

# Open chip and request line
chip = gpiod.Chip(CHIP)
line = chip.get_line(PIN)
line.request(consumer="button-test", type=gpiod.LINE_REQ_EV_FALLING_EDGE, default_val=1)

print("✅ Listening for button presses on GPIO17 (Ctrl+C to exit)")

last_time = 0
is_on = True
counter = 1
try:
    while is_on:
        if line.event_wait(1):  # waits up to 1 second for an event
            event = line.event_read()
            now = time.time()
            if now - last_time > DEBOUNCE_TIME:
                print(f"Button pressed! Event at {time.strftime('%H:%M:%S')}")
                counter+=1
                last_time = now
                if counter > 4:
                    is_on = False
except KeyboardInterrupt:
    print("\nStopped by user.")
finally:
    line.release()
    chip.close()