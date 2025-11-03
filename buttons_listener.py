#!/usr/bin/env python3
# buttons_4_daemon_spec.py — libgpiod 1.x, Prints-Flag + Daemon-Events nach Hearo-Spec
import gpiod, time, socket, json, os

CHIP = "gpiochip0"
POLL_SLEEP = 0.01   # 10 ms Polling
DEBOUNCE_MS = 30
PRINTS_ON = True
SOCKET_PATH = "/tmp/hearo_buttons.sock"   # {{SOCKET_PATH}}

# Times per spec
CLICK_HOLD_MS = 300          # ab hier gilt "Pressed"
REPEAT_NEXTPREV_MS = 500     # Seek-Repeat alle 500 ms
REPEAT_VOL_MS = 300          # Volume-Repeat alle 300 ms
RESET_HOLD_MS = 5000         # Reset ab 5 s Hold
VOL_STEP = 5                 # Prozentpunkte pro Schritt
SEEK_STEP_SEC = 15           # Sekunden pro Schritt

# Pin -> (Name, Action)
# Actions: next, prev, vol_up, vol_down, reset
# Actions: "next", "prev", "vol_up", "vol_down", "reset", or None (ignorieren)
BUTTONS = {
    # Aktiv genutzte Buttons
    17: ("Btn A", "next"),
    22: ("Btn B", "prev"),
    23: ("Btn C", "vol_up"),
    27: ("Btn D", "vol_down"),

    # Optional: Reset auf eigenem Pin (Beispiel-Pin anpassen)
    # {{reset_pin}}: ("Reset", "reset"),

    # Weitere gängige BCM-Pins vorbereitet (standard: ignorieren)
    2:  ("Unused", None),
    3:  ("Unused", None),
    4:  ("Unused", None),
    5:  ("Unused", None),
    6:  ("Unused", None),
    12: ("Unused", None),
    13: ("Unused", None),
    16: ("Unused", None),
    18: ("Unused", None),
    19: ("Unused", None),
    20: ("Unused", None),
    21: ("Unused", None),
    24: ("Unused", None),
    25: ("Unused", None),
    26: ("Unused", None),
}

def log(msg): 
    if PRINTS_ON: 
        print(msg)

def send_event(payload: dict):
    """Sendet JSON-Event an lokalen Daemon (UNIX DGRAM)."""
    data = json.dumps(payload).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(SOCKET_PATH)
            s.send(data)
    except Exception as e:
        if PRINTS_ON:
            print(f"(warn) Event not sent: {e}")

def now_ms() -> float:
    return time.time() * 1000.0

class Btn:
    def __init__(self, chip, pin, name, action):
        self.pin, self.name, self.action = pin, name, action
        self.line = chip.get_line(pin)
        self.line.request(
            consumer=f"btn-{pin}",
            type=gpiod.LINE_REQ_DIR_IN,
            flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,  # intern. Pull-Up aktiv
        )
        self.state_last = self.line.get_value()  # 1 = released, 0 = pressed
        self.t_last_change = now_ms()
        self.t_down = 0.0
        self.hold_started = False
        self.t_last_repeat = 0.0

    def _emit_click(self):
        if self.action == "next":
            log(f"Klick - {self.name} → NEXT")
            send_event({"type":"player.next","ts":time.time()})
        elif self.action == "prev":
            log(f"Klick - {self.name} → PREV")
            send_event({"type":"player.prev","ts":time.time()})
        elif self.action == "vol_up":
            log(f"Klick - {self.name} → VOL +{VOL_STEP}%")
            send_event({"type":"player.volume.step","delta":+VOL_STEP,"ts":time.time()})
        elif self.action == "vol_down":
            log(f"Klick - {self.name} → VOL -{VOL_STEP}%")
            send_event({"type":"player.volume.step","delta":-VOL_STEP,"ts":time.time()})
        elif self.action == "reset":
            # Klick macht bei Reset nichts (nur Hold ≥ 5 s)
            log(f"Klick - {self.name} (ignoriert für RESET)")

    def _emit_hold_start(self):
        log(f"Pressed start - {self.name}")
        send_event({"type":"button.pressed_start","button":self.name,"ts":time.time()})
        # optional: sofortige erste Aktion bei Hold
        self._emit_hold_repeat(initial=True)

    def _emit_hold_repeat(self, initial=False):
        # Wiederholte Aktion abhängig von self.action
        if self.action == "next":
            if initial:
                # auf Hold-Start noch nicht seeken, erst nach 500 ms
                return
            log(f"Pressed - {self.name} → SEEK +{SEEK_STEP_SEC}s")
            send_event({"type":"player.seek","delta_sec":+SEEK_STEP_SEC,"ts":time.time()})
        elif self.action == "prev":
            if initial:
                return
            log(f"Pressed - {self.name} → SEEK -{SEEK_STEP_SEC}s")
            send_event({"type":"player.seek","delta_sec":-SEEK_STEP_SEC,"ts":time.time()})
        elif self.action == "vol_up":
            log(f"Pressed - {self.name} → VOL +{VOL_STEP}%")
            send_event({"type":"player.volume.step","delta":+VOL_STEP,"ts":time.time()})
        elif self.action == "vol_down":
            log(f"Pressed - {self.name} → VOL -{VOL_STEP}%")
            send_event({"type":"player.volume.step","delta":-VOL_STEP,"ts":time.time()})
        elif self.action == "reset":
            # Bei Reset keine Repeats, nur Prüfung auf 5 s in update()
            pass

    def _emit_hold_end(self):
        log(f"Pressed end - {self.name}")
        send_event({"type":"button.pressed_end","button":self.name,"ts":time.time()})

    def update(self):
        now = now_ms()
        state = self.line.get_value()
        # Entprellte Edge-Erkennung
        if state != self.state_last and (now - self.t_last_change) > DEBOUNCE_MS:
            self.t_last_change = now
            if state == 0:  # gedrückt
                self.t_down = now
                self.hold_started = False
                self.t_last_repeat = 0.0
            else:          # losgelassen
                held_ms = now - self.t_down
                if self.action == "reset" and held_ms >= RESET_HOLD_MS:
                    log(f"RESET ausgelöst - {self.name}")
                    send_event({"type":"system.reset","ts":time.time()})
                else:
                    if not self.hold_started and held_ms < CLICK_HOLD_MS:
                        self._emit_click()
                    elif self.hold_started:
                        self._emit_hold_end()
            self.state_last = state

        # Hold-Start & Repeats während gehalten
        if self.state_last == 0:  # weiterhin gedrückt
            held_ms = now - self.t_down
            if not self.hold_started and held_ms >= CLICK_HOLD_MS:
                self.hold_started = True
                self.t_last_repeat = now
                self._emit_hold_start()
            elif self.hold_started:
                interval = REPEAT_VOL_MS if self.action in ("vol_up","vol_down") \
                          else REPEAT_NEXTPREV_MS
                if (now - self.t_last_repeat) >= interval:
                    self.t_last_repeat = now
                    self._emit_hold_repeat()

def main():
    chip = gpiod.Chip(CHIP)
    # --- Aufbau der Button-Objekte nur für aktive Pins ---
    btns = [Btn(chip, pin, name, action) for pin, (name, action) in BUTTONS.items() if action]

    log("Listening for button events (Ctrl+C to exit)")
    try:
        while True:
            for b in btns:
                b.update()
            time.sleep(POLL_SLEEP)
    except KeyboardInterrupt:
        pass
    finally:
        for b in btns: b.line.release()
        chip.close()

if __name__ == "__main__":
    main()