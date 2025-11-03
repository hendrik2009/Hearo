#!/usr/bin/env python3
# buttons_4.py — libgpiod 1.x
import gpiod, time

CHIP = "gpiochip0"
HOLD_MS = 300
REPEAT_MS = 200
DEBOUNCE_MS = 30

# BCM-Pins -> Anzeigename
BUTTONS = {
    17: "Btn A",
    22: "Btn B",
    23: "Btn C",
    27: "Btn D",
}

class Btn:
    def __init__(self, chip, pin, name):
        self.pin = pin
        self.name = name
        self.line = chip.get_line(pin)
        self.line.request(consumer=f"btn-{pin}",
                          type=gpiod.LINE_REQ_EV_BOTH_EDGES,
                          default_val=1)
        self.pressed = False         # logisch: 0 = gedrückt (active-low)
        self.t_down = 0.0
        self.t_last_edge = 0.0
        self.hold_started = False
        self.t_last_repeat = 0.0

    def handle_edge(self, ev):
        now = time.time() * 1000.0   # ms
        if now - self.t_last_edge < DEBOUNCE_MS:
            return
        self.t_last_edge = now

        if ev.type == gpiod.LineEvent.FALLING_EDGE:
            # 1 -> 0 : Taste gedrückt
            self.pressed = True
            self.t_down = now
            self.hold_started = False
            self.t_last_repeat = 0.0
        elif ev.type == gpiod.LineEvent.RISING_EDGE and self.pressed:
            # 0 -> 1 : Taste losgelassen
            if not self.hold_started and (now - self.t_down) < HOLD_MS:
                print(f"Klick - {self.name}")
            else:
                print(f"Pressed end - {self.name}")
            self.pressed = False
            self.hold_started = False
            self.t_down = 0.0
            self.t_last_repeat = 0.0

    def tick(self):
        """Periodische Aktionen: Hold-Start und Repeats."""
        if not self.pressed:
            return
        now = time.time() * 1000.0
        # Hold-Start nach HOLD_MS
        if not self.hold_started and (now - self.t_down) >= HOLD_MS:
            self.hold_started = True
            self.t_last_repeat = now
            print(f"Pressed start - {self.name}")
            print(f"Pressed - {self.name}")  # sofortige erste Wiederholung
        # Wiederholungen alle REPEAT_MS
        elif self.hold_started and (now - self.t_last_repeat) >= REPEAT_MS:
            self.t_last_repeat = now
            print(f"Pressed - {self.name}")

def main():
    chip = gpiod.Chip(CHIP)
    btns = [Btn(chip, pin, name) for pin, name in BUTTONS.items()]
    print("Lauschend auf Taster (Ctrl+C zum Beenden)")

    try:
        while True:
            # Warte kurz auf Events aller Lines
            for b in btns:
                if b.line.event_wait(0.02):   # 20 ms
                    ev = b.line.event_read()
                    b.handle_edge(ev)
            # Periodische Checks (Hold/Repeat)
            for b in btns:
                b.tick()
    except KeyboardInterrupt:
        pass
    finally:
        for b in btns:
            b.line.release()
        chip.close()

if __name__ == "__main__":
    main()