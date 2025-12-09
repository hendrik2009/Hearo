#!/usr/bin/env python3
"""
HEARO – Power Daemon (POWD) – minimal stub

- Binds a command socket (ignored for now)
- Emits:
    POWD_EVENT_DAEMON_STARTED        (once on startup)
    POWD_EVENT_BATTERY_STATE {...}   (periodic, mocked values)

No real hardware; SoC / bands are fixed to a "good" state.
"""

import os
import sys
import json
import time
import socket
import signal
import logging
from typing import Dict, Any, Optional

EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"
POWD_CMD_SOCKET_PATH = "/tmp/hearo/powd.sock"

IPC_SCHEMA_EVENT = "hearo.ipc/event"
IPC_SCHEMA_CMD = "hearo.ipc/cmd"


def epoch_ms() -> int:
    return int(time.time() * 1000.0)


def send_event(event: str, payload: Dict[str, Any]) -> None:
    env = {
        "schema": IPC_SCHEMA_EVENT,
        "v": 1,
        "id": f"evt-powd-{epoch_ms()}",
        "ts": epoch_ms(),
        "event": event,
        "payload": payload or {},
    }
    data = json.dumps(env, separators=(",", ":")).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(EVENT_SOCKET_PATH)
            s.send(data)
    except OSError as e:
        logging.error("POWD: failed to send event %s: %s", event, e)


class PowdStub:
    def __init__(self,
                 cmd_path: str = POWD_CMD_SOCKET_PATH,
                 heartbeat_sec: int = 30) -> None:
        self.cmd_path = cmd_path
        self.heartbeat_sec = heartbeat_sec
        self.running = True
        self.cmd_sock: Optional[socket.socket] = None

    def setup(self) -> None:
        # Bind command socket (ignore contents for now)
        os.makedirs(os.path.dirname(self.cmd_path), exist_ok=True)
        try:
            if os.path.exists(self.cmd_path):
                os.unlink(self.cmd_path)
        except OSError:
            pass

        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(self.cmd_path)
        s.setblocking(False)
        self.cmd_sock = s
        logging.info("POWD: cmd socket bound at %s", self.cmd_path)

        # Initial daemon-started event
        send_event("POWD_EVENT_DAEMON_STARTED", {})
        logging.info("POWD: sent POWD_EVENT_DAEMON_STARTED")

    def loop(self) -> None:
        next_heartbeat = time.time()

        while self.running:
            # Ignore any incoming commands for now
            if self.cmd_sock is not None:
                try:
                    self.cmd_sock.recvfrom(65535)
                except BlockingIOError:
                    pass
                except OSError as e:
                    logging.warning("POWD: recv error: %s", e)

            now = time.time()
            if now >= next_heartbeat:
                self._emit_battery_state()
                next_heartbeat = now + self.heartbeat_sec

            time.sleep(0.2)

    def _emit_battery_state(self) -> None:
        # Mocked "healthy" state
        payload = {
            "soc": 80,              # 80% state of charge
            "band": "BAT_NORM",     # from spec: BAT_NORM / BAT_LOW / BAT_CRIT / BAT_CHG
            "ext_power": False,     # running on battery
            "temp_band": "TEMP_OK", # from spec: TEMP_OK / TEMP_WARN / TEMP_CRIT
        }
        send_event("POWD_EVENT_BATTERY_STATE", payload)
        logging.info("POWD: sent BATTERY_STATE %s", payload)

    def stop(self) -> None:
        self.running = False
        if self.cmd_sock is not None:
            try:
                self.cmd_sock.close()
            except Exception:
                pass
            self.cmd_sock = None
        # No DAEMON_STOPPED yet; can be added later
        logging.info("POWD: stopped")


def configure_logging() -> None:
    level = logging.INFO
    if "--debug" in sys.argv or "--verbose" in sys.argv:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] POWD: %(message)s",
    )


def main() -> int:
    configure_logging()
    stub = PowdStub()

    def _sig_handler(signum, frame):
        logging.info("POWD: signal %s, stopping", signum)
        stub.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    stub.setup()
    stub.loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
