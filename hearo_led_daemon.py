#!/usr/bin/env python3
"""
Hearo LED Daemon – GPIO12, PWM1, smooth WS2811

- 1+ WS281x LEDs on GPIO12 (PWM1 CH0)
- IPC on /tmp/hearo/ledd.sock
- Background, feedback, error layers
"""

import os
import sys
import json
import time
import math
import signal
import logging
import socket
import selectors
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any

from rpi_ws281x import PixelStrip, Color

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CMD_SOCKET_PATH = "/tmp/hearo/ledd.sock"
EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"

IPC_SCHEMA_EVENT = "hearo.ipc/event"

TICK_HZ = 30.0
TICK_INTERVAL = 1.0 / TICK_HZ

LED_COUNT = 1          # set to actual number later
LED_PIN = 12           # GPIO12 (PWM1 CH0)
LED_FREQ_HZ = 800000
LED_DMA = 10
LED_INVERT = False
LED_GLOBAL_BRIGHTNESS = 64
LED_CHANNEL = 0

ERROR_PERIOD_MS = 10_000
ERROR_BRIGHTNESS = 160

Mode = Literal["steady", "wave"]
Shape = Literal["square", "smooth", "fade_in", "fade_out"]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RGB:
    r: int
    g: int
    b: int


@dataclass
class Animation:
    mode: Mode
    color: RGB
    brightness: int

    shape: Optional[Shape] = None
    period_ms: Optional[int] = None
    duty_cycle: float = 0.5
    cycles: Optional[int] = None

    start_ms: int = 0
    cycles_done: int = 0
    duration_ms: Optional[int] = None


@dataclass
class DaemonState:
    current_state: Animation
    feedback: Optional[Animation]
    error_active: bool
    error_start_ms: Optional[int] = None


# ---------------------------------------------------------------------------
# LED driver
# ---------------------------------------------------------------------------

class LedDriver:
    def __init__(self, led_count: int):
        self.led_count = led_count

        self.strip = PixelStrip(
            LED_COUNT,
            LED_PIN,
            LED_FREQ_HZ,
            LED_DMA,
            LED_INVERT,
            LED_GLOBAL_BRIGHTNESS,
            LED_CHANNEL
        )
        self.strip.begin()
        logging.info("PixelStrip initialized: pin=%d, brightness=%d",
                     LED_PIN, LED_GLOBAL_BRIGHTNESS)

        # One clean off frame
        for i in range(self.led_count):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()
        time.sleep(0.02)

    def show_color(self, r: int, g: int, b: int):
        c = Color(r, g, b)
        for i in range(self.led_count):
            self.strip.setPixelColor(i, c)
        self.strip.show()
        logging.debug("LED -> (%3d,%3d,%3d)", r, g, b)

    def off(self):
        self.show_color(0, 0, 0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def now_ms():
    return int(time.monotonic() * 1000)


def hsv_to_rgb(h, s, v) -> RGB:
    h = h % 1.0
    i = int(h * 6)
    f = (h * 6) - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    i = i % 6

    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q

    return RGB(int(r * 255), int(g * 255), int(b * 255))


# ---------------------------------------------------------------------------
# Animation
# ---------------------------------------------------------------------------

def compute_wave_factor(anim: Animation, t_ms: int) -> float:
    if anim.mode == "steady":
        return 1.0

    if not anim.period_ms or anim.period_ms <= 0:
        return 1.0

    elapsed = t_ms - anim.start_ms
    period = anim.period_ms
    phase = (elapsed % period) / period
    anim.cycles_done = elapsed // period

    if anim.shape == "square":
        return 1.0 if phase < clamp(anim.duty_cycle, 0, 1) else 0.0
    if anim.shape == "fade_in":
        return clamp(elapsed / period, 0.0, 1.0)
    if anim.shape == "fade_out":
        return clamp(1 - (elapsed / period), 0.0, 1.0)

    # smooth
    return 0.5 - 0.5 * math.cos(2 * math.pi * phase)


def expire_feedback(state: DaemonState, t_ms: int):
    fb = state.feedback
    if not fb:
        return
    if fb.duration_ms is not None and t_ms - fb.start_ms >= fb.duration_ms:
        state.feedback = None
        return
    if fb.cycles is not None and fb.cycles_done >= fb.cycles:
        state.feedback = None


def compute_active_rgb(state: DaemonState, t_ms: int) -> RGB:
    if state.error_active:
        if state.error_start_ms is None:
            state.error_start_ms = t_ms
        elapsed = (t_ms - state.error_start_ms) % ERROR_PERIOD_MS
        hue = elapsed / ERROR_PERIOD_MS
        val = ERROR_BRIGHTNESS / 255.0
        return hsv_to_rgb(hue, 1.0, val)

    expire_feedback(state, t_ms)
    anim = state.feedback if state.feedback else state.current_state

    base = anim.color
    f = (anim.brightness / 255.0) * compute_wave_factor(anim, t_ms)

    return RGB(
        int(base.r * f),
        int(base.g * f),
        int(base.b * f)
    )


# ---------------------------------------------------------------------------
# Event sender (to HCSM / shared events)
# ---------------------------------------------------------------------------

class EventSender:
    def __init__(self, path: str):
        self.path = path

    def send_event(self, event: str, payload: Dict[str, Any]):
        env = {
            "schema": IPC_SCHEMA_EVENT,
            "v": 1,
            "id": f"evt-ledd-{now_ms()}",
            "ts": now_ms(),
            "event": event,
            "payload": payload or {},
        }
        data = json.dumps(env, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.path)
                s.send(data)
        except OSError as e:
            logging.warning("LEDD: failed to send event %s: %s", event, e)


# ---------------------------------------------------------------------------
# IPC
# ---------------------------------------------------------------------------

class IpcServer:
    def __init__(self, path: str):
        self.path = path
        self.selector = selectors.DefaultSelector()
        self.server = None

    def start(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            os.unlink(self.path)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(8)
        srv.setblocking(False)
        self.selector.register(srv, selectors.EVENT_READ, self._accept)
        self.server = srv
        logging.info("IPC: listening on %s", self.path)

    def _accept(self, sock):
        conn, _ = sock.accept()
        conn.setblocking(False)
        logging.debug("IPC: accepted connection")
        self.selector.register(conn, selectors.EVENT_READ, self._read)

    def _read(self, conn):
        try:
            data = conn.recv(4096)
        except Exception as e:
            logging.warning("IPC: read error: %s", e)
            data = None

        if not data:
            self.selector.unregister(conn)
            conn.close()
            return

        try:
            txt = data.decode("utf-8")
            logging.debug("IPC: raw data: %r", txt)
            msg = json.loads(txt)
            IPC_MESSAGE_QUEUE.append(msg)
        except Exception as e:
            logging.warning("IPC: bad JSON: %s", e)

        self.selector.unregister(conn)
        conn.close()

    def poll(self):
        events = self.selector.select(timeout=0.01)
        for key, _ in events:
            cb = key.data
            cb(key.fileobj)

    def close(self):
        try:
            self.selector.close()
        except Exception:
            pass
        try:
            if self.server:
                self.server.close()
        except Exception:
            pass
        if os.path.exists(self.path):
            os.unlink(self.path)


IPC_MESSAGE_QUEUE: list[Dict[str, Any]] = []


# ---------------------------------------------------------------------------
# ACK / RESULT
# ---------------------------------------------------------------------------

def send_reply(path: str, payload: Dict[str, Any]):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.sendall(json.dumps(payload).encode("utf-8"))
        s.close()
    except Exception as e:
        logging.warning("IPC: failed to send reply to %s: %s", path, e)


def send_ack(msg: Dict[str, Any], ok: bool = True):
    reply = msg.get("reply")
    if not reply:
        return
    send_reply(reply, {
        "schema": "hearo.ipc/ack",
        "id": msg.get("id"),
        "ok": ok,
        "ts": now_ms()
    })


def send_result(msg: Dict[str, Any], payload: Dict[str, Any]):
    reply = msg.get("reply")
    if not reply:
        return
    send_reply(reply, {
        "schema": "hearo.ipc/result",
        "id": msg.get("id"),
        "ok": True,
        "payload": payload,
        "ts": now_ms()
    })


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

def parse_rgb(d) -> Optional[RGB]:
    if not isinstance(d, dict):
        return None
    try:
        return RGB(
            int(clamp(d["r"], 0, 255)),
            int(clamp(d["g"], 0, 255)),
            int(clamp(d["b"], 0, 255)),
        )
    except Exception:
        return None


def handle_cmd(msg: Dict[str, Any], state: DaemonState):
    logging.info("CMD: %s", msg.get("cmd"))
    if msg.get("schema") != "hearo.ipc/cmd":
        send_ack(msg, False)
        return

    cmd = msg.get("cmd")
    p = msg.get("payload") or {}

    if cmd == "LED_SET_STATE":
        c = parse_rgb(p.get("color"))
        if not c:
            send_ack(msg, False)
            return
        anim = Animation(
            mode=p.get("mode", "steady"),
            color=c,
            brightness=int(p.get("brightness", 255)),
            shape=p.get("shape"),
            period_ms=p.get("period_ms"),
            duty_cycle=float(p.get("duty_cycle", 0.5)),
            cycles=None,
            start_ms=now_ms()
        )
        state.current_state = anim
        send_ack(msg, True)

    elif cmd == "LED_SET_FEEDBACK":
        c = parse_rgb(p.get("color"))
        if not c:
            send_ack(msg, False)
            return
        anim = Animation(
            mode=p.get("mode", "wave"),
            color=c,
            brightness=int(p.get("brightness", 255)),
            shape=p.get("shape", "smooth"),
            period_ms=p.get("period_ms", 500),
            duty_cycle=float(p.get("duty_cycle", 0.5)),
            cycles=p.get("cycles"),
            duration_ms=p.get("duration_ms"),
            start_ms=now_ms()
        )
        state.feedback = anim
        send_ack(msg, True)

    elif cmd == "LED_SET_ERROR":
        state.error_active = bool(p.get("enabled"))
        state.error_start_ms = None
        send_ack(msg, True)

    elif cmd == "LED_OFF":
        state.current_state = Animation(
            mode="steady",
            color=RGB(0, 0, 0),
            brightness=0,
            start_ms=now_ms()
        )
        state.feedback = None
        state.error_active = False
        send_ack(msg, True)

    elif cmd == "LED_PING":
        send_ack(msg, True)
        send_result(msg, {
            "feedback_active": state.feedback is not None,
            "error_active": state.error_active
        })

    else:
        send_ack(msg, False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

RUNNING = True


def _stop(signum, frame):
    global RUNNING
    RUNNING = False


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    driver = LedDriver(LED_COUNT)

    state = DaemonState(
        current_state=Animation("steady", RGB(0, 0, 0), 0, start_ms=now_ms()),
        feedback=None,
        error_active=False
    )

    ipc = IpcServer(CMD_SOCKET_PATH)
    ipc.start()

    # Send DAEMON_STARTED event for HCSM
    sender = EventSender(EVENT_SOCKET_PATH)
    sender.send_event("LEDD_EVENT_DAEMON_STARTED", {})

    last_tick = time.monotonic()

    try:
        while RUNNING:
            ipc.poll()

            while IPC_MESSAGE_QUEUE:
                msg = IPC_MESSAGE_QUEUE.pop(0)
                handle_cmd(msg, state)

            now = time.monotonic()
            if now - last_tick < TICK_INTERVAL:
                time.sleep(TICK_INTERVAL - (now - last_tick))
                continue
            last_tick = now

            rgb = compute_active_rgb(state, now_ms())
            driver.show_color(rgb.r, rgb.g, rgb.b)

    finally:
        driver.off()
        ipc.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
