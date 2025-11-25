#!/usr/bin/env python3
"""
HEARO Button Daemon (BD)

Production implementation aligned with:
- HEARO Button Daemon (BD) V1
- HEARO IPC Message Scheme V1.3
- Hardware GPIO mapping (NEXT, PREV, VOL_UP, VOL_DOWN, RESET)

Dependencies:
- Python 3.x
- libgpiod 1.x (python bindings)

This daemon:
- Reads the 5 physical buttons via libgpiod
- Debounces edges and measures press duration
- Classifies interactions into SHORT_PRESS / LONG_PRESS / HOLD_TICK
- Emits BD_EVENT_* events on /tmp/hearo/events.sock
- Listens for BD_CMD_PING / BD_CMD_SET_DEBUG on /tmp/hearo/bd.sock
"""

import os
import sys
import time
import json
import socket
import signal
import logging
import errno
from typing import Dict, Optional, Any, List

import gpiod

# ------------------------------
# Configuration (defaults)
# ------------------------------

CHIP_NAME = "gpiochip0"

# GPIO mapping (BCM) -> logical button name
BUTTON_GPIO_MAP: Dict[int, str] = {
    17: "NEXT",
    22: "PREV",
    23: "VOL_UP",
    27: "VOL_DOWN",
    24: "RESET",
}

# Timing thresholds (ms) – per BD spec
DEBOUNCE_MS = 30
SHORT_MIN_MS = 50
LONG_THRESHOLD_MS = 800
RESET_LONG_THRESHOLD_MS = 5000
HOLD_TICK_INTERVAL_MS = 250

# IPC sockets
EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"
CMD_SOCKET_PATH = "/tmp/hearo/bd.sock"

# Main loop
POLL_INTERVAL_SEC = 0.01  # 10 ms


# ------------------------------
# IPC helpers
# ------------------------------

_evt_counter = 0
_ack_counter = 0
_res_counter = 0


def epoch_ms() -> int:
    return int(time.time() * 1000.0)


def make_event_envelope(event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    global _evt_counter
    _evt_counter += 1
    return {
        "schema": "hearo.ipc/event",
        "v": 1,
        "id": f"evt-bd-{_evt_counter}",
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


class EventSender:
    """
    Thin wrapper around a Unix datagram socket used to send events
    to /tmp/hearo/events.sock.
    """

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
      IDLE -> DEBOUNCE_PRESS -> PRESSED -> LONG_HELD -> DEBOUNCE_RELEASE -> IDLE
    """

    STATE_IDLE = "IDLE"
    STATE_DEBOUNCE_PRESS = "DEBOUNCE_PRESS"
    STATE_PRESSED = "PRESSED"
    STATE_LONG_HELD = "LONG_HELD"
    STATE_DEBOUNCE_RELEASE = "DEBOUNCE_RELEASE"

    def __init__(self, name: str, line: gpiod.Line, is_reset: bool = False) -> None:
        self.name = name
        self.line = line
        self.is_reset = is_reset

        # raw level: 1 = released (pull-up), 0 = pressed
        self.last_level = 1
        self.state = self.STATE_IDLE

        self.t_last_change_ms = epoch_ms()
        self.t_press_start_ms = 0
        self.t_last_hold_tick_ms = 0

        self.sequence_counter = 0

    def _next_sequence(self) -> int:
        self.sequence_counter += 1
        return self.sequence_counter

    def read_level(self) -> int:
        try:
            return self.line.get_value()
        except OSError as e:
            logging.error("BD: GPIO read failed for %s: %s", self.name, e)
            # Treat as released on error to avoid stuck-pressed
            return 1

    def update(self, now_ms: int, sender: EventSender, bd_context: Dict[str, Any]) -> None:
        """
        Poll function called from main loop.
        Updates internal state and emits BD_EVENT_BUTTON for:
          - SHORT_PRESS
          - LONG_PRESS
          - HOLD_TICK
        """
        level = self.read_level()

        # Edge detection with debounce (just track when level last changed)
        if level != self.last_level:
            self.t_last_change_ms = now_ms
            self.last_level = level

        stable_duration = now_ms - self.t_last_change_ms

        # FSM
        if self.state == self.STATE_IDLE:
            if level == 0 and stable_duration >= DEBOUNCE_MS:
                # Confirmed press
                self.state = self.STATE_PRESSED
                self.t_press_start_ms = now_ms
                self.t_last_hold_tick_ms = now_ms
                logging.debug("BD: %s -> PRESSED", self.name)

        elif self.state == self.STATE_PRESSED:
            press_duration = now_ms - self.t_press_start_ms

            if level == 1 and stable_duration >= DEBOUNCE_MS:
                # Released before long threshold -> candidate SHORT_PRESS
                if SHORT_MIN_MS <= press_duration < LONG_THRESHOLD_MS:
                    self._emit_button_event(
                        sender,
                        interaction="SHORT_PRESS",
                        duration_ms=press_duration,
                        bd_context=bd_context,
                    )
                # else: ignore too-short noise
                self.state = self.STATE_IDLE
                logging.debug("BD: %s -> IDLE (release, dur=%d ms)", self.name, press_duration)

            elif press_duration >= (RESET_LONG_THRESHOLD_MS if self.is_reset else LONG_THRESHOLD_MS):
                # Transition into LONG_HELD
                self.state = self.STATE_LONG_HELD
                logging.debug("BD: %s -> LONG_HELD", self.name)

        elif self.state == self.STATE_LONG_HELD:
            press_duration = now_ms - self.t_press_start_ms

            # HOLD_TICKs while still pressed
            if level == 0:
                if now_ms - self.t_last_hold_tick_ms >= HOLD_TICK_INTERVAL_MS:
                    self.t_last_hold_tick_ms = now_ms
                    self._emit_button_event(
                        sender,
                        interaction="HOLD_TICK",
                        duration_ms=press_duration,
                        bd_context=bd_context,
                    )
            # Release after long hold -> LONG_PRESS
            elif level == 1 and stable_duration >= DEBOUNCE_MS:
                threshold = RESET_LONG_THRESHOLD_MS if self.is_reset else LONG_THRESHOLD_MS
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

        # No explicit DEBOUNCE_RELEASE state: debouncing is done via stable_duration checks.

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
            "duration_ms": int(duration_ms),
            "sequence": seq,
        }
        env = make_event_envelope("BD_EVENT_BUTTON", payload)
        sender.send(env)

        # Update BD context for BD_CMD_PING
        bd_context["last_button"] = payload
        logging.info(
            "BD: %s %s (dur=%d ms, seq=%d)",
            self.name,
            interaction,
            duration_ms,
            seq,
        )


# ------------------------------
# Command server
# ------------------------------

class CommandServer:
    """
    Unix datagram command server bound to CMD_SOCKET_PATH.

    Expects "hearo.ipc/cmd" envelopes with:
      cmd: "BD_CMD_PING" or "BD_CMD_SET_DEBUG"
    """

    def __init__(self, path: str, sender: EventSender, bd_context: Dict[str, Any]) -> None:
        self.path = path
        self.sender = sender
        self.bd_context = bd_context

        # Remove stale socket file if present
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.error("BD: failed to unlink existing cmd socket %s: %s", self.path, e)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)

    def fileno(self) -> int:
        return self.sock.fileno()

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def handle_one(self) -> None:
        try:
            data, addr = self.sock.recvfrom(4096)
        except OSError as e:
            if e.errno != errno.EINTR:
                logging.error("BD: error reading cmd socket: %s", e)
            return

        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception as e:
            logging.warning("BD: invalid JSON on cmd socket: %s", e)
            return

        schema = msg.get("schema")
        if schema != "hearo.ipc/cmd":
            logging.warning("BD: ignoring non-cmd schema: %r", schema)
            return

        cmd_id = msg.get("id") or "cmd-unknown"
        cmd = msg.get("cmd")
        payload = msg.get("payload") or {}
        reply_path = msg.get("reply")

        if not reply_path:
            logging.warning("BD: cmd without reply path (id=%s)", cmd_id)
            return

        if cmd == "BD_CMD_PING":
            self._handle_ping(cmd_id, reply_path)
        elif cmd == "BD_CMD_SET_DEBUG":
            self._handle_set_debug(cmd_id, payload, reply_path)
        else:
            self._send_unknown_cmd(cmd_id, reply_path, cmd)

    def _send_ipc(self, path: str, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(path)
                s.send(data)
        except OSError as e:
            logging.error("BD: failed to send cmd response to %s: %s", path, e)

    def _handle_ping(self, cmd_id: str, reply_path: str) -> None:
        # ACK
        ack = make_ack_envelope(cmd_id, ok=True, error=None)
        self._send_ipc(reply_path, ack)

        # RESULT
        uptime_ms = int((time.monotonic() - self.bd_context["t_start_monotonic"]) * 1000)
        result_payload = {
            "ok": True,
            "payload": {
                "status": self.bd_context.get("status", "ready"),
                "last_button": self.bd_context.get("last_button"),
                "last_error_code": self.bd_context.get("last_error_code"),
                "uptime_ms": uptime_ms,
            },
        }
        res = make_result_envelope(cmd_id, ok=True, payload=result_payload)
        self._send_ipc(reply_path, res)

    def _handle_set_debug(self, cmd_id: str, payload: Dict[str, Any], reply_path: str) -> None:
        level_str = str(payload.get("level", "")).lower()
        mapping = {
            "none": logging.CRITICAL + 1,
            "error": logging.ERROR,
            "warn": logging.WARNING,
            "warning": logging.WARNING,
            "info": logging.INFO,
            "debug": logging.DEBUG,
        }
        level = mapping.get(level_str)
        ok = level is not None

        if ok:
            logging.getLogger().setLevel(level)
            logging.info("BD: log level set to %s", level_str)
            error = None
        else:
            error = f"invalid level '{level_str}'"

        ack = make_ack_envelope(cmd_id, ok=ok, error=error)
        self._send_ipc(reply_path, ack)

        result_payload = {
            "ok": ok,
            "payload": {
                "applied_level": level_str if ok else None,
                "error": error,
            },
        }
        res = make_result_envelope(cmd_id, ok=ok, payload=result_payload)
        self._send_ipc(reply_path, res)

    def _send_unknown_cmd(self, cmd_id: str, reply_path: str, cmd: Any) -> None:
        error = f"unknown command '{cmd}'"
        ack = make_ack_envelope(cmd_id, ok=False, error=error)
        self._send_ipc(reply_path, ack)

        result_payload = {
            "ok": False,
            "payload": {
                "error": error,
            },
        }
        res = make_result_envelope(cmd_id, ok=False, payload=result_payload)
        self._send_ipc(reply_path, res)


# ------------------------------
# Main daemon
# ------------------------------

class ButtonDaemon:
    def __init__(self) -> None:
        self.event_sender = EventSender(EVENT_SOCKET_PATH)
        self.bd_context: Dict[str, Any] = {
            "status": "init",
            "last_button": None,
            "last_error_code": None,
            "t_start_monotonic": time.monotonic(),
        }

        self.chip: Optional[gpiod.Chip] = None
        self.buttons: List[ButtonState] = []
        self.cmd_server: Optional[CommandServer] = None
        self.running = True

    # --- setup/teardown ---

    def setup(self) -> None:
        # Setup GPIO chip and button lines
        try:
            self.chip = gpiod.Chip(CHIP_NAME)
        except OSError as e:
            self._report_error("GPIO_INIT_FAILED", f"chip open failed: {e}", recovering=False)
            raise SystemExit(1)

        try:
            for gpio, logical_name in BUTTON_GPIO_MAP.items():
                line = self.chip.get_line(gpio)
                line.request(
                    consumer=f"bd-{logical_name.lower()}",
                    type=gpiod.LINE_REQ_DIR_IN,
                    flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
                )
                self.buttons.append(
                    ButtonState(
                        name=logical_name,
                        line=line,
                        is_reset=(logical_name == "RESET"),
                    )
                )
                logging.info("BD: configured button %s on GPIO%d", logical_name, gpio)
        except OSError as e:
            self._report_error("GPIO_INIT_FAILED", f"line request failed: {e}", recovering=False)
            raise SystemExit(1)

        # Setup command server
        self.cmd_server = CommandServer(CMD_SOCKET_PATH, self.event_sender, self.bd_context)

        # Emit DAEMON_STARTED
        started_payload = {
            "version": "1.0",
            "pid": os.getpid(),
        }
        self.event_sender.send(make_event_envelope("BD_EVENT_DAEMON_STARTED", started_payload))

        self.bd_context["status"] = "ready"
        logging.info("BD: daemon started, status=ready")

    def teardown(self, reason: str) -> None:
        logging.info("BD: shutting down (%s)", reason)

        try:
            stopped_payload = {
                "reason": reason,
                "pid": os.getpid(),
            }
            self.event_sender.send(make_event_envelope("BD_EVENT_DAEMON_STOPPED", stopped_payload))
        except Exception:
            pass

        if self.cmd_server is not None:
            self.cmd_server.close()

        for btn in self.buttons:
            try:
                btn.line.release()
            except Exception:
                pass

        if self.chip is not None:
            try:
                self.chip.close()
            except Exception:
                pass

    def _report_error(self, code: str, message: str, recovering: bool) -> None:
        self.bd_context["status"] = "error"
        self.bd_context["last_error_code"] = code
        payload = {
            "code": code,
            "message": message,
            "recovering": bool(recovering),
        }
        self.event_sender.send(make_event_envelope("BD_EVENT_ERROR", payload))
        logging.error("BD: error %s (recovering=%s): %s", code, recovering, message)

    # --- main loop ---

    def run(self) -> None:
        import select

        self.setup()

        # Signal handling
        def handle_signal(signum, frame):
            logging.info("BD: received signal %s, stopping", signum)
            self.running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            while self.running:
                now_ms = epoch_ms()

                # Update buttons
                for btn in self.buttons:
                    btn.update(now_ms, self.event_sender, self.bd_context)

                # Handle commands (non-blocking wait with timeout = POLL_INTERVAL_SEC)
                if self.cmd_server is not None:
                    rlist, _, _ = select.select(
                        [self.cmd_server.fileno()],
                        [],
                        [],
                        POLL_INTERVAL_SEC,
                    )
                    if rlist:
                        self.cmd_server.handle_one()
                else:
                    time.sleep(POLL_INTERVAL_SEC)

        except Exception as e:
            self._report_error("INTERNAL_EXCEPTION", str(e), recovering=False)
            raise
        finally:
            self.teardown(reason="signal" if not self.running else "exception")


# ------------------------------
# Entry point
# ------------------------------

def configure_logging() -> None:
    level = logging.INFO
    if "-v" in sys.argv or "--verbose" in sys.argv or "--debug" in sys.argv:
        level = logging.DEBUG
    logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] bd: %(message)s",
    )


def main() -> None:
    configure_logging()
    daemon = ButtonDaemon()
    daemon.run()


if __name__ == "__main__":
    main()
