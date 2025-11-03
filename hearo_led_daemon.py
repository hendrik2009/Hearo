#!/usr/bin/env python3
# hearo_led_daemon.py — LED-State+Feedback-Daemon (libgpiod 1.x)
import os, time, json, socket, select, threading, queue, math
import gpiod

# ===== Konfiguration =====
SOCKET_PATH = "/tmp/hearo_led.sock"     # IPC: hier empfängt der Daemon
PRINTS_ON   = True

# GPIO / PWM
CHIP = "gpiochip0"
PIN_RED, PIN_GREEN, PIN_BLUE = 12, 13, 18     # HW-PWM-freundliche BCM-Pins
COMMON_CATHODE = True                          # True: an=1, aus=0 (LED an GND)
PWM_HZ = 400

def log(s): 
    if PRINTS_ON: print(s, flush=True)

# ===== Soft-PWM + RGB =====
class SoftPWM:
    def __init__(self, chip, pin, freq=400, active_high=True):
        self.line = chip.get_line(pin)
        self.line.request(consumer=f"pwm-{pin}", type=gpiod.LINE_REQ_DIR_OUT)
        self.period = 1.0 / max(10, freq)
        self.active_high = active_high
        self._duty = 0.0
        self._want = 0.0
        self._stop = False
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def set(self, duty):  # 0..1
        self._want = 0.0 if duty < 0 else (1.0 if duty > 1 else float(duty))

    def _loop(self):
        while not self._stop:
            # einfache Glättung gegen Flackern bei Duty-Änderung
            self._duty += (self._want - self._duty) * 0.4
            on_time = self._duty * self.period
            off_time = self.period - on_time
            # ON
            self.line.set_value(1 if self.active_high else 0)
            if on_time > 0: time.sleep(on_time)
            # OFF
            self.line.set_value(0 if self.active_high else 1)
            if off_time > 0: time.sleep(off_time)

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

    def set(self, r, g, b):   # 0..1
        self.r.set(r); self.g.set(g); self.b.set(b)

    def off(self):
        self.set(0,0,0)

    def close(self):
        self.r.close(); self.g.close(); self.b.close()

# ===== Pattern-Generatoren =====
def pat_static(color, dur_s):
    r,g,b = color
    t0 = time.time()
    while time.time() - t0 < dur_s:
        yield (r,g,b, 0.02)

def pat_blink(color, on_s, off_s, cycles=3):
    r,g,b = color
    for _ in range(cycles):
        t0 = time.time()
        while time.time() - t0 < on_s:  yield (r,g,b, 0.02)
        t0 = time.time()
        while time.time() - t0 < off_s: yield (0,0,0, 0.02)

def pat_pulse(color, period_s=1.5, cycles=3):
    r,g,b = color
    steps = max(30, int(period_s/0.02))
    for _ in range(cycles):
        for i in range(steps):
            x = (1 - math.cos(2*math.pi*(i/steps))) * 0.5  # 0..1..0
            yield (r*x, g*x, b*x, 0.02)

# ===== Farben & Mappings (aus Spezifikation abgeleitet) =====
COLORS = {
    "red":(1,0,0), "green":(0,1,0), "blue":(0,0,1),
    "yellow":(1,1,0), "magenta":(1,0,1), "white":(1,1,1), "off":(0,0,0)
}

PATTERNS = {
    # Dauerzustände (Beispiele)
    "idle_blue":        lambda: pat_static(COLORS["blue"], 2.0),          # Netzbetrieb/pausiert
    "wifi_missing":     lambda: pat_pulse(COLORS["yellow"], 2.0, 9999),   # endlos (wird im State-Loop erneut aufgerufen)
    # Situative Feedbacks
    "battery_low":      lambda: pat_blink(COLORS["yellow"], 0.2, 1.8, 5),
    "battery_critical": lambda: pat_blink(COLORS["red"],   1.0, 1.0, 6),
    "charging":         lambda: pat_pulse(COLORS["green"], 1.2, 6),
    "confirm":          lambda: pat_blink(COLORS["green"], 0.08, 0.08, 3),
    "error":            lambda: pat_blink(COLORS["red"],   0.1,  0.1, 5),
    "no_net":           lambda: pat_blink(COLORS["magenta"],0.2, 0.8, 6),
}

# ===== Renderer + Worker =====
class LedRenderer:
    """Exklusiver Zugriff via Lock: Feedback übersteuert den State temporär."""
    def __init__(self, rgb: RGB):
        self.rgb = rgb
        self.lock = threading.RLock()

    def play_pattern(self, pat_callable):
        with self.lock:
            for r,g,b,dt in pat_callable():
                self.rgb.set(r,g,b); time.sleep(dt)
            self.rgb.off()

    def play_color(self, color, dur):
        with self.lock:
            t0 = time.time()
            while time.time() - t0 < float(dur):
                self.rgb.set(*color); time.sleep(0.02)
            self.rgb.off()

class StateWorker(threading.Thread):
    """Endloser State-Renderer. Pausiert automatisch, wenn Feedback lockt."""
    def __init__(self, renderer: LedRenderer, state_getter):
        super().__init__(daemon=True)
        self.renderer = renderer
        self.state_getter = state_getter
        self._stop = False

    def run(self):
        while not self._stop:
            pat = self.state_getter()
            if not pat or pat not in PATTERNS:
                time.sleep(0.1); continue
            try:
                # pro „Zyklus“ des Patterns (z. B. Puls) einmal durchspielen
                with self.renderer.lock:
                    for r,g,b,dt in PATTERNS[pat]():
                        self.renderer.rgb.set(r,g,b); time.sleep(dt)
                # kein off(): State soll sichtbar bleiben
            except Exception:
                time.sleep(0.05)

    def stop(self): self._stop = True

class FeedbackWorker(threading.Thread):
    """Abarbeitung der One-Shots (FIFO)."""
    def __init__(self, renderer: LedRenderer, q: queue.Queue):
        super().__init__(daemon=True)
        self.renderer = renderer
        self.q = q
        self._stop = False

    def run(self):
        while not self._stop:
            try:
                job = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if job.get("pattern") in PATTERNS:
                    self.renderer.play_pattern(PATTERNS[job["pattern"]])
                elif job.get("color"):
                    self.renderer.play_color(job["color"], job.get("dur",1.0))
            finally:
                self.q.task_done()

    def stop(self): self._stop = True

# ===== IPC =====
def ensure_socket(path):
    try:
        if os.path.exists(path): os.unlink(path)
    except OSError: pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    os.chmod(path, 0o666)  # ggf. restriktiver (0o660) + passende Gruppe
    return s

# ===== Main =====
def main():
    chip = gpiod.Chip(CHIP)
    rgb = RGB(chip, PIN_RED, PIN_GREEN, PIN_BLUE, common_cathode=COMMON_CATHODE, freq=PWM_HZ)
    renderer = LedRenderer(rgb)

    _state = {"name": "idle_blue"}          # Startzustand
    def get_state(): return _state["name"]

    feedback_q = queue.Queue(maxsize=64)
    sw = StateWorker(renderer, get_state); sw.start()
    fw = FeedbackWorker(renderer, feedback_q); fw.start()

    sock = ensure_socket(SOCKET_PATH)
    log(f"LED daemon ready on {SOCKET_PATH} (R{PIN_RED}/G{PIN_GREEN}/B{PIN_BLUE})")

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
                pat = evt.get("pattern")
                if pat in PATTERNS:
                    _state["name"] = pat
                    log(f"state -> {pat}")
                else:
                    log(f"(warn) unknown state: {pat}")
            elif t == "led.feedback":
                pat = evt.get("pattern")
                if pat in PATTERNS:
                    feedback_q.put({"pattern": pat})
                    log(f"feedback queued: {pat}")
                else:
                    log(f"(warn) unknown feedback: {pat}")
            elif t == "led.raw":
                color = evt.get("color"); dur = evt.get("dur",1.0)
                if isinstance(color, list) and len(color)==3:
                    feedback_q.put({"color": color, "dur": dur})
                    log(f"raw queued: {color} / {dur}s")
                else:
                    log("(warn) invalid raw color payload")
            else:
                # Beispielhafte Systemevent-Mappings (optional)
                m = {
                    "system.no_net": "no_net",
                    "system.charging": "charging",
                    "player.paused": "idle_blue",
                    "battery.low": "battery_low",
                    "battery.critical": "battery_critical",
                    "confirm": "confirm",
                    "error": "error",
                }
                if t in m and m[t] in PATTERNS:
                    feedback_q.put({"pattern": m[t]})
                    log(f"mapped {t} -> {m[t]}")
                else:
                    log(f"(info) ignored event: {evt}")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close(); os.unlink(SOCKET_PATH)
        except Exception: pass
        sw.stop(); fw.stop()
        renderer.rgb.off(); renderer.rgb.close(); chip.close()
        log("LED daemon stopped.")

if __name__ == "__main__":
    main()
