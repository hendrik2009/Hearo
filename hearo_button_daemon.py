#!/usr/bin/env python3
"""
HEARO Button Daemon (BD) – libgpiod 2.x implementation

- Uses GPIO pins as per Hardware_GPIO Documentation V1.2:
    NEXT      -> GPIO17 (active-low, pull-up)
    PREV      -> GPIO22 (active-low, pull-up)
    VOL_UP    -> GPIO23 (active-low, pull-up)
    VOL_DOWN  -> GPIO27 (active-low, pull-up)
    RESET     -> GPIO24 (active-low, pull-up, ≥5 s) :contentReference[oaicite:1]{index=1}

- Classifies interactions according to BD spec:
    SHORT_PRESS, LONG_PRESS, HOLD_TICK :contentReference[oaicite:2]{index=2}

- IPC:
    Events  -> /tmp/hearo/events.sock
    Commands-> /tmp/hearo/bd.sock :contentReference[oaicite:3]{index=3}

No extra functionality beyond the spec.
"""

import argparse
import json
import logging
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import gpiod
from gpiod.line import Direction, Bias, Value  # libgpiod 2.x enums 


# ------------------------------
# Global constants (from BD spec)
# ------------------------------

EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"
CMD_SOCKET_PATH = "/tmp/hearo/bd.sock"

# Timing (ms) :contentReference[oaicite:5]{index=5}
DEBOUNCE_MS = 30
SHORT_MIN_MS = 50
LONG_THRESHOLD_MS = 800
RESET_LONG_THRESHOLD_MS = 5000
HOLD_TICK_INTERVAL_MS = 250

POLL_INTERVAL_MS = 10  # main loop poll period

# GPIO mapping (BCM) :contentReference[oaicite:6]{index=6}
BUTTON_PINS = {
    "NEXT": 17,
    "PREV": 22,
    "VOL_UP": 23,
    "VOL_DOWN": 27,
    "RESET": 24,
}


# ------------------------------
# Utility: time, IPC envelopes
# ------------------------------

def epoch_ms() -> int:
    return int(time.time() * 1000)


_event_counter = 0
_ack_counter = 0
_res_counter = 0


def _next_event_id() -> str:
    global _event_counter
    _event_counter += 1
    return f"e-bd-{_event_counter}"


def make_event_envelope(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "hearo.ipc/event",
        "v": 1,
        "id": _next_event_id(),
        "ts": epoch_ms(),
        "event": event,
        "payload": payload,
    }


def make_ack_envelope(cmd_id: str, ok: bool, error: Optional[str] = None) -> Dict[str, Any]:
    global _ack_counter
    _ack_counter += 1
    return {
        "schema": "hearo.ipc/ack",
        "v": 1,
        "id": f"ack-bd-{_ack_counter}",
        "ts": epoch_ms(),
        "corr": cmd_id,
        "ok": bool(ok),
        "error": error,
    }


def make_result_envelope(cmd_id: str, ok: bool, payload: Dict[str, Any]) -> Dict[str, Any]:
    global _res_counter
    _res_counter += 1
    return {
        "schema": "hearo.ipc/result",
        "v": 1,
        "id": f"res-bd-{_res_counter}",
        "ts": epoch_ms(),
        "corr": cmd_id,
        "ok": bool(ok),
        "payload": payload,
    }


# ------------------------------
# Event sender
# ------------------------------

class EventSender:
    """Thin wrapper around a Unix datagram socket used to send events."""

    def __init__(self, path: str) -> None:
        self.path = path

    def send(self, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.path)
                s.send(data)
        except OSError as e:
            logging.warning("BD: failed to send event to %s: %s", self.path, e)


# ------------------------------
# Button state machine
# ------------------------------

class ButtonState:
    """
    Per-button debouncing and interaction classification.

    Implements states:
      IDLE -> PRESSED -> LONG_HELD -> IDLE
    (release debounced via stable duration)
    """

    STATE_IDLE = "IDLE"
    STATE_PRESSED = "PRESSED"
    STATE_LONG_HELD = "LONG_HELD"

    def __init__(self, name: str, offset: int, is_reset: bool = False) -> None:
        self.name = name
        self.offset = offset
        self.is_reset = is_reset

        # raw logical level: 1 = released, 0 = pressed
        self.last_level = 1
        self.state = self.STATE_IDLE

        self.t_last_change_ms = epoch_ms()
        self.t_press_start_ms = 0
        self.t_last_hold_tick_ms = 0

        self.sequence_counter = 0

    def _next_sequence(self) -> int:
        self.sequence_counter += 1
        return self.sequence_counter

    def _emit_button_event(
        self,
        sender: EventSender,
        interaction: str,
        duration_ms: int,
        bd_context: Dict[str, Any],
    ) -> None:
        seq = self._next_sequence()
        payload = {
            "button": self.name,
            "interaction": interaction,
            "duration_ms": duration_ms,
            "sequence": seq,
        }
        event = make_event_envelope("BD_EVENT_BUTTON", payload)
        sender.send(event)
        bd_context["last_button"] = payload
        logging.info(
            "BD: BUTTON %s %s duration=%dms seq=%d",
            self.name,
            interaction,
            duration_ms,
            seq,
        )

    def update(
        self,
        now_ms: int,
        current_level: int,
        sender: EventSender,
        bd_context: Dict[str, Any],
    ) -> None:
        """
        Poll function called from main loop.

        current_level: 1=released, 0=pressed (logical, after mapping from gpiod.Value).
        """
        # Edge detection with debounce
        if current_level != self.last_level:
            self.t_last_change_ms = now_ms
            self.last_level = current_level

        stable_duration = now_ms - self.t_last_change_ms

        if self.state == self.STATE_IDLE:
            if current_level == 0 and stable_duration >= DEBOUNCE_MS:
                # Confirmed press
                self.state = self.STATE_PRESSED
                self.t_press_start_ms = now_ms
                self.t_last_hold_tick_ms = now_ms
                logging.debug("BD: %s -> PRESSED", self.name)

        elif self.state == self.STATE_PRESSED:
            press_duration = now_ms - self.t_press_start_ms

            if current_level == 1 and stable_duration >= DEBOUNCE_MS:
                # Released before long threshold -> candidate SHORT_PRESS
                if SHORT_MIN_MS <= press_duration < (
                    RESET_LONG_THRESHOLD_MS if self.is_reset else LONG_THRESHOLD_MS
                ):
                    self._emit_button_event(
                        sender,
                        interaction="SHORT_PRESS",
                        duration_ms=press_duration,
                        bd_context=bd_context,
                    )
                # else: ignore too-short noise
                self.state = self.STATE_IDLE
                logging.debug(
                    "BD: %s -> IDLE (release, dur=%d ms)", self.name, press_duration
                )

            else:
                threshold = (
                    RESET_LONG_THRESHOLD_MS if self.is_reset else LONG_THRESHOLD_MS
                )
                if press_duration >= threshold:
                    self.state = self.STATE_LONG_HELD
                    logging.debug("BD: %s -> LONG_HELD", self.name)

        elif self.state == self.STATE_LONG_HELD:
            press_duration = now_ms - self.t_press_start_ms

            # HOLD_TICKs while still pressed
            if current_level == 0:
                if now_ms - self.t_last_hold_tick_ms >= HOLD_TICK_INTERVAL_MS:
                    self.t_last_hold_tick_ms = now_ms
                    self._emit_button_event(
                        sender,
                        interaction="HOLD_TICK",
                        duration_ms=press_duration,
                        bd_context=bd_context,
                    )
            # Release after long hold -> LONG_PRESS
            elif current_level == 1 and stable_duration >= DEBOUNCE_MS:
                threshold = (
                    RESET_LONG_THRESHOLD_MS if self.is_reset else LONG_THRESHOLD_MS
                )
                if press_duration >= threshold:
                    self._emit_button_event(
                        sender,
                        interaction="LONG_PRESS",
                        duration_ms=press_duration,
                        bd_context=bd_context,
                    )
                else:
                    # fallback: still treat as SHORT_PRESS if above min
                    if press_duration >= SHORT_MIN_MS:
                        self._emit_button_event(
                            sender,
                            interaction="SHORT_PRESS",
                            duration_ms=press_duration,
                            bd_context=bd_context,
                        )
                self.state = self.STATE_IDLE
                logging.debug(
                    "BD: %s -> IDLE (release after LONG_HELD, dur=%d ms)",
                    self.name,
                    press_duration,
                )


# ------------------------------
# Command server
# ------------------------------

class CommandServer:
    """
    Listens on /tmp/hearo/bd.sock for JSON IPC commands and replies via
    caller-provided reply sockets, strictly following BD spec. 
    """

    def __init__(self, path: str, sender: EventSender, ctx: Dict[str, Any]) -> None:
        self.path = path
        self.sender = sender
        self.ctx = ctx

        # Ensure directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)

        # Remove stale socket
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(0.1)

    def close(self) -> None:
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _send_reply(self, reply_path: str, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(reply_path)
                s.send(data)
        except OSError as e:
            logging.warning("BD: failed to send reply to %s: %s", reply_path, e)

    def _handle_cmd_ping(self, cmd: Dict[str, Any], reply: str) -> None:
        cmd_id = cmd.get("id", "")
        ack = make_ack_envelope(cmd_id, ok=True, error=None)
        self._send_reply(reply, ack)

        result_payload = {
            "status": self.ctx.get("status", "ready"),
            "last_button": self.ctx.get("last_button"),
            "last_error_code": self.ctx.get("last_error_code"),
            "uptime_ms": epoch_ms() - self.ctx.get("start_time_ms", epoch_ms()),
        }
        res = make_result_envelope(cmd_id, ok=True, payload=result_payload)
        self._send_reply(reply, res)

    def _handle_cmd_set_debug(self, cmd: Dict[str, Any], reply: str) -> None:
        cmd_id = cmd.get("id", "")
        level = cmd.get("payload", {}).get("level")
        mapping = {
            "none": logging.CRITICAL,
            "error": logging.ERROR,
            "warn": logging.WARNING,
            "info": logging.INFO,
            "debug": logging.DEBUG,
        }
        if level not in mapping:
            ack = make_ack_envelope(cmd_id, ok=False, error="INVALID_LEVEL")
            self._send_reply(reply, ack)
            return

        logging.getLogger().setLevel(mapping[level])
        ack = make_ack_envelope(cmd_id, ok=True, error=None)
        self._send_reply(reply, ack)

        res = make_result_envelope(cmd_id, ok=True, payload={"level": level})
        self._send_reply(reply, res)

    def poll(self) -> None:
        try:
            data, _addr = self.sock.recvfrom(4096)
        except socket.timeout:
            return
        except OSError as e:
            logging.error("BD: command socket error: %s", e)
            self.ctx["last_error_code"] = "CMD_SOCKET_ERROR"
            return

        try:
            cmd = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            logging.warning("BD: received invalid JSON on cmd socket")
            return

        if cmd.get("schema") != "hearo.ipc/cmd":
            logging.warning("BD: ignoring non-cmd message")
            return

        reply = cmd.get("reply")
        if not reply:
            logging.warning("BD: cmd without reply path")
            return

        name = cmd.get("cmd")
        if name == "BD_CMD_PING":
            self._handle_cmd_ping(cmd, reply)
        elif name == "BD_CMD_SET_DEBUG":
            self._handle_cmd_set_debug(cmd, reply)
        else:
            cmd_id = cmd.get("id", "")
            ack = make_ack_envelope(cmd_id, ok=False, error="UNKNOWN_COMMAND")
            self._send_reply(reply, ack)


# ------------------------------
# libgpiod 2.x GPIO wrapper
# ------------------------------

@dataclass
class GpioRequest:
    request: gpiod.LineRequest
    idx_map: Dict[str, int]  # button name -> index in get_values() list


def setup_gpio() -> GpioRequest:
    """
    Request all button lines as inputs with pull-up, active_low=True,
    using libgpiod 2.x request_lines API.
    """
    chip_path = "/dev/gpiochip0"

    # fixed order of offsets for the request
    offsets_order = list(BUTTON_PINS.values())

    settings = gpiod.LineSettings(
        direction=Direction.INPUT,
        bias=Bias.PULL_UP,
        active_low=True,
    )

    config = {
        tuple(offsets_order): settings,
    }

    req = gpiod.request_lines(
        chip_path,
        consumer="hearo-bd",
        config=config,
    )

    # map button name -> index in returned list
    idx_map: Dict[str, int] = {}
    for name, offset in BUTTON_PINS.items():
        idx_map[name] = offsets_order.index(offset)

    return GpioRequest(request=req, idx_map=idx_map)


def read_button_levels(gpio_req: GpioRequest) -> Dict[str, int]:
    """
    Read all button lines and convert to:
        1 = released
        0 = pressed
    """
    try:
        raw = gpio_req.request.get_values()  # list[Value]
    except OSError as e:
        logging.error("BD: GPIO get_values failed: %s", e)
        return {name: 1 for name in BUTTON_PINS.keys()}

    levels: Dict[str, int] = {}
    for name, idx in gpio_req.idx_map.items():
        try:
            v = raw[idx]
        except IndexError:
            v = Value.INACTIVE
        levels[name] = 0 if v == Value.ACTIVE else 1
    return levels


# ------------------------------
# Main daemon
# ------------------------------

class ButtonDaemon:
    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self.sender = EventSender(EVENT_SOCKET_PATH)
        self.ctx: Dict[str, Any] = {
            "status": "init",
            "last_button": None,
            "last_error_code": None,
            "start_time_ms": epoch_ms(),
        }

        self.cmd_server: Optional[CommandServer] = None
        self.gpio_req: Optional[GpioRequest] = None
        self.buttons: Dict[str, ButtonState] = {}

        self._stopping = False

    def setup(self) -> None:
        # IPC command server
        self.cmd_server = CommandServer(CMD_SOCKET_PATH, self.sender, self.ctx)

        # GPIO
        try:
            self.gpio_req = setup_gpio()
        except Exception as e:
            logging.error("BD: failed to set up GPIO: %s", e)
            self.ctx["last_error_code"] = "GPIO_INIT_FAILED"
            self.ctx["status"] = "error"
            err_evt = make_event_envelope(
                "BD_EVENT_ERROR",
                {"code": "GPIO_INIT_FAILED", "message": str(e)},
            )
            self.sender.send(err_evt)
            raise

        # Button state objects – use BUTTON_PINS for offsets
        for name, offset in BUTTON_PINS.items():
            self.buttons[name] = ButtonState(
                name=name,
                offset=offset,
                is_reset=(name == "RESET"),
            )

        self.ctx["status"] = "ready"

        # Emit DAEMON_STARTED
        started_evt = make_event_envelope("BD_EVENT_DAEMON_STARTED", {})
        self.sender.send(started_evt)
        logging.info("BD: daemon started")

    def stop(self) -> None:
        self._stopping = True

    def run(self) -> None:
        self.setup()

        assert self.cmd_server is not None
        assert self.gpio_req is not None

        try:
            while not self._stopping:
                now = epoch_ms()

                # Read all button levels
                levels = read_button_levels(self.gpio_req)

                # Update per-button FSM
                for name, button in self.buttons.items():
                    level = levels.get(name, 1)
                    button.update(now, level, self.sender, self.ctx)

                # Handle commands
                self.cmd_server.poll()

                time.sleep(POLL_INTERVAL_MS / 1000.0)
        finally:
            # Emit DAEMON_STOPPED
            stopped_evt = make_event_envelope("BD_EVENT_DAEMON_STOPPED", {})
            self.sender.send(stopped_evt)
            logging.info("BD: daemon stopping")

            if self.cmd_server:
                self.cmd_server.close()

            if self.gpio_req:
                try:
                    self.gpio_req.request.release()
                except Exception:
                    pass


# ------------------------------
# Entry point
# ------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="HEARO Button Daemon (libgpiod 2.x)")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    daemon = ButtonDaemon(debug=args.debug)

    def handle_signal(signum, frame):
        logging.info("BD: received signal %s, stopping", signum)
        daemon.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        daemon.run()
    except KeyboardInterrupt:
        daemon.stop()
    except Exception as e:
        logging.exception("BD: fatal error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
