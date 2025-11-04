#!/usr/bin/env python3
# LED-Daemon: States (dauerhaft) + Feedbacks (sofort, einmalig), libgpiod 1.x
import os, time, json, socket, select, threading
import gpiod

# ===== Konfiguration =====
SOCKET_PATH = "/tmp/hearo_led.sock"
PRINTS_ON   = True

CHIP = "gpiochip0"
PIN_RED, PIN_GREEN, PIN_BLUE = 12, 13, 18   # anpassen
COMMON_CATHODE = True                       # True: 1=an, 0=aus (Common Cathode)
PWM_HZ = 400
DT = 0.02                                   # 20ms Schrittweite

def log(s): 
    if PRINTS_ON: print(s, flush=True)

# ===== Minimal-SoftPWM (sanft & leicht) =====
class SoftPWM:
    def __init__(self, chip, pin, freq=400, active_high=True):
        self.line = chip.get_line(pin)
        self.line.request(consumer=f"pwm-{pin}", type=gpiod.LINE_REQ_DIR_OUT)
        self.period = 1.0 / max(60, freq)
        self.active_high = active_high
        self._duty = 0.0
        self._target = 0.0
        self._stop = False
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def set(self, duty):            # 0..1
        self._target = 0.0 if duty < 0 else (1.0 if duty > 1 else float(duty))

    def _loop(self):
        while not self._stop:
            # kleine Glättung
            self._duty += (self._target - self._duty) * 0.4
            on_t  = self._duty * self.period
            off_t = self.period - on_t
            self.line.set_value(1 if self.active_high else 0)
            if on_t  > 0: time.sleep(on_t)
            self.line.set_value(0 if self.active_high else 1)
            if off_t > 0: time.sleep(off_t)

    def close(self):
        self._stop = True
        self._th.join(timeout=0.5)
        self.line.set_value(0 if self.active_high else 1)
        self.line.release()

class RGB:
    def __init__(self, chip, r, g, b, common_cathode=True, freq=PWM_HZ):
        ah = True if common_cathode else False
        self.r = SoftPWM(chip, r, freq=freq, active_high=ah)
        self.g = SoftPWM(chip, g, freq=freq, active_high=ah)
        self.b = SoftPWM(chip, b, freq=freq, active_high=ah)

    def set(self, r, g, b): self.r.set(r); self.g.set(g); self.b.set(b)
    def off(self): self.set(0,0,0)
    def close(self): self.r.close(); self.g.close(); self.b.close()

# ===== Helligkeit / Luminanz =====
def apply_luminance(color, L):
    L = max(0.2, min(1.0, float(L)))
    r,g,b = color
    return (r*L, g*L, b*L)

# ===== CSS-ähnliche Keyframe-Profile =====
def mode_steady(color, period):
    # konstante Farbe (period wird ignoriert)
    while True:
        yield color; time.sleep(DT)

def mode_blink(color, period):
    # CSS: blinker { 50% {opacity:0} }
    # → 0–50% an, 50–100% aus
    T = max(0.4, float(period))
    t = 0.0
    while True:
        phase = (t % T) / T
        if phase < 0.5:  yield color
        else:            yield (0,0,0)
        time.sleep(DT); t += DT

def mode_pulse(color, period):
    # CSS:
    # pulser: 0%..30% op=1, 50% op=0, 70% op=1, 100% op=1
    # Wir interpolieren linear zwischen den Keyframes
    T = max(0.8, float(period))
    t = 0.0
    while True:
        p = (t % T) / T  # 0..1
        if p <= 0.30:    op = 1.0
        elif p <= 0.50:  # 0.30->1  to 0.50->0
            op = 1.0 - (p-0.30)/(0.20)*1.0
        elif p <= 0.70:  # 0.50->0  to 0.70->1
            op = (p-0.50)/(0.20)*1.0
        else:            op = 1.0
        r,g,b = color
        yield (r*op, g*op, b*op)
        time.sleep(DT); t += DT

# ===== Defaults (States & Feedbacks) =====
COL = {
    "red":(1,0,0), "green":(0,1,0), "blue":(0,0,1),
    "yellow":(1,1,0), "magenta":(1,0,1), "white":(1,1,1), "off":(0,0,0)
}

STATE_DEFAULTS = {
    # name: (color, mode, luminance, period_s)
    "off":             (COL["off"],    "steady", 0.0, 1.0),
    "idle_green":      (COL["green"],  "steady", 0.4, 1.0),
    "idle_blue":       (COL["blue"],   "steady", 0.4, 1.0),
    "charging":        (COL["green"],  "pulse",  0.4, 1.5),
    "wifi_missing":    (COL["yellow"], "pulse",  0.4, 3.0),
    "battery_low":     (COL["yellow"], "blink",  0.4, 2.0),
    "battery_critical":(COL["red"],    "blink",  0.4, 1.0),
}

FEEDBACK_DEFAULTS = {
    # kurze Sequenzen; period steuert Tempo, Dauer entsteht aus Pattern
    # "name": ( color,       mode,      luminance, period_s, repeats )
    "confirm": (COL["green"], "blink", 1.0, 0.3, 2), # 2x kurz blinken
    "error":   (COL["red"],   "blink", 1.0, 0.20, 3), # 3x kurz blinken
}

# ===== Controller ohne Queue =====
class LedController:
    def __init__(self, rgb: RGB):
        self.rgb = rgb
        self._lock = threading.RLock()
        self._run = True

        # State
        self.state = {"color":COL["blue"], "mode":"steady", "L":0.4, "period":1.0}
        # Feedback (sofort, übersteuert laufendes Feedback)
        self._fb_id = 0
        self._fb_req = None  # tuple(kind, id, generator_fn)

        self._t_state = threading.Thread(target=self._state_loop, daemon=True)
        self._t_fb    = threading.Thread(target=self._feedback_loop, daemon=True)
        self._t_state.start(); self._t_fb.start()

    # --- Public API ---
    def set_state(self, *, color=None, mode=None, luminance=None, period=None, preset=None):
        if preset:
            c,m,L,T = STATE_DEFAULTS.get(preset, (None,None,None,None))
            if c is not None:
                self.state.update({"color":c, "mode":m, "L":L, "period":T})
                log(f"state -> {preset} ({m}, L={L}, T={T})")
                return
        if color:      self.state["color"] = tuple(color)
        if mode:       self.state["mode"] = mode
        if luminance:  self.state["L"] = float(luminance)
        if period:     self.state["period"] = float(period)
        log(f"state -> custom {self.state}")

    def play_feedback(self, *, preset=None, color=None, mode=None, luminance=None, period=None, repeats=None, dur=None):
        with self._lock:
            self._fb_id += 1
            fid = self._fb_id

            # Generator für Feedback zusammenstellen
            if preset:
                c,m,L,T,rep = FEEDBACK_DEFAULTS[preset]
                c = apply_luminance(c, L)
                gen = self._build_generator(m, c, T, repeats=rep)
            else:
                c0 = apply_luminance(color if color else COL["white"], luminance if luminance else 1.0)
                if mode == "steady":
                    # dur: Sekunden (Default 0.6s)
                    d = float(dur if dur else 0.6)
                    def gen():
                        t0 = time.time()
                        while time.time()-t0 < d:
                            yield c0; time.sleep(DT)
                else:
                    T = float(period if period else 1.0)
                    gen = self._build_generator(mode or "blink", c0, T, repeats=repeats if repeats else 3)

            self._fb_req = ("pattern", fid, gen)

    # --- interne Helfer ---
    def _build_generator(self, mode, color, period, repeats=3):
        if mode == "blink":
            # 1 cycle = T; CSS: 50% off
            def gen():
                for _ in range(repeats):
                    # 0..50% an
                    t = 0.0
                    while t < period*0.5: yield color; time.sleep(DT); t+=DT
                    # 50..100% aus
                    t = 0.0
                    while t < period*0.5: yield (0,0,0); time.sleep(DT); t+=DT
            return gen
        elif mode == "pulse":
            # CSS pulser: 0-30% 1.0, 50% 0.0, 70% 1.0
            def gen():
                for _ in range(repeats):
                    t = 0.0
                    while t < period:
                        p = t/period
                        if p <= 0.30:    op = 1.0
                        elif p <= 0.50:  op = 1.0 - (p-0.30)/0.20
                        elif p <= 0.70:  op = (p-0.50)/0.20
                        else:            op = 1.0
                        r,g,b = color; yield (r*op, g*op, b*op); time.sleep(DT); t+=DT
            return gen
        else:  # steady
            def gen():
                # ~repeats Sekunden
                t0 = time.time()
                while time.time()-t0 < repeats:
                    yield color; time.sleep(DT)
            return gen

    # --- Loops ---
    def _state_loop(self):
        while self._run:
            # State Schritt generieren
            c  = apply_luminance(self.state["color"], self.state["L"])
            m  = self.state["mode"]
            T  = self.state["period"]
            # Taste einen *kleinen* Schritt, damit Feedback jederzeit preempten kann
            if m == "steady":
                step = c
            elif m == "blink":
                # 50% an / 50% aus
                phase = (time.time() % T) / T
                step = c if phase < 0.5 else (0,0,0)
            elif m == "pulse":
                p = (time.time() % T) / T
                if   p <= 0.30: op = 1.0
                elif p <= 0.50: op = 1.0 - (p-0.30)/0.20
                elif p <= 0.70: op = (p-0.50)/0.20
                else:           op = 1.0
                r,g,b = c; step = (r*op, g*op, b*op)
            else:
                step = c

            # Nur setzen, wenn kein Feedback läuft
            if self._fb_req is None and self._lock.acquire(timeout=0.001):
                try: self.rgb.set(*step)
                finally: self._lock.release()
            time.sleep(DT)

    def _feedback_loop(self):
        while self._run:
            req = None
            with self._lock:
                if self._fb_req is not None:
                    req = self._fb_req
            if not req:
                time.sleep(0.005); continue

            kind, fid, gen = req
            if self._lock.acquire(timeout=0.02):
                try:
                    # Übersteuerung prüfen (neueres Feedback?)
                    if fid != self._fb_id: 
                        continue
                    for step in gen():
                        if fid != self._fb_id:  # neues Feedback eingetroffen
                            break
                        self.rgb.set(*step)
                finally:
                    self._lock.release()
                    if fid == self._fb_id:
                        self._fb_req = None
            else:
                time.sleep(0.005)

    def stop(self):
        self._run = False

# ===== IPC =====
def ensure_socket(path):
    try:
        if os.path.exists(path): os.unlink(path)
    except OSError: pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path); os.chmod(path, 0o666)
    return s

def main():
    chip = gpiod.Chip(CHIP)
    rgb  = RGB(chip, PIN_RED, PIN_GREEN, PIN_BLUE, common_cathode=COMMON_CATHODE, freq=PWM_HZ)
    ctl  = LedController(rgb)
    sock = ensure_socket(SOCKET_PATH)
    log(f"LED daemon ready on {SOCKET_PATH} (R{PIN_RED}/G{PIN_GREEN}/B{PIN_BLUE})")

    # Startzustand
    c,m,L,T = STATE_DEFAULTS["idle_blue"]
    ctl.set_state(color=c, mode=m, luminance=L, period=T)

    try:
        while True:
            r,_,_ = select.select([sock], [], [], 1.0)
            if not r: continue
            try:
                data,_ = sock.recvfrom(4096)
                evt = json.loads(data.decode("utf-8","replace").strip())
            except Exception as e:
                log(f"(warn) invalid payload: {e}"); continue

            t = evt.get("type","")
            if t == "led.state":
                # Varianten:
                # 1) {"pattern":"idle_blue"}
                # 2) {"color":[r,g,b], "mode":"pulse", "luminance":0.6, "period":1.5}
                pat = evt.get("pattern")
                if pat:
                    if pat in STATE_DEFAULTS:
                        ctl.set_state(preset=pat)
                    else:
                        log(f"(warn) unknown state preset: {pat}")
                else:
                    ctl.set_state(
                        color=evt.get("color"),
                        mode=evt.get("mode"),
                        luminance=evt.get("luminance"),
                        period=evt.get("period"),
                    )
            elif t == "led.feedback":
                # Varianten:
                # 1) {"pattern":"confirm"}
                # 2) {"color":[1,1,1], "mode":"blink", "period":0.15, "repeats":4}
                pat = evt.get("pattern")
                if pat:
                    if pat in FEEDBACK_DEFAULTS:
                        ctl.play_feedback(preset=pat)
                    elif pat == "off":
                        ctl.play_feedback(color=(0,0,0), mode="steady", dur=0.2)
                    else:
                        log(f"(warn) unknown feedback preset: {pat}")
                else:
                    ctl.play_feedback(
                        color=evt.get("color"),
                        mode=evt.get("mode","blink"),
                        luminance=evt.get("luminance",1.0),
                        period=evt.get("period",1.0),
                        repeats=evt.get("repeats",3),
                        dur=evt.get("dur"),
                    )
            else:
                log(f"(info) unknown msg: {evt}")
    except KeyboardInterrupt:
        pass
    finally:
        try: sock.close(); os.unlink(SOCKET_PATH)
        except Exception: pass
        ctl.stop(); rgb.off(); rgb.close(); chip.close()
        log("LED daemon stopped.")

if __name__ == "__main__":
    main()
