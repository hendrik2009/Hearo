#!/usr/bin/env python3
"""
HEARO – Central State Machine (HCSM) daemon

Implements HCSM V3 with adjustments:

- Global system states:
    SYS_INIT, SYS_NO_WIFI, SYS_OFFLINE,
    SYS_READY_PAUSED, SYS_PLAYING, SYS_SHUTDOWN, SYS_ERROR

- Inputs (events):
    NFC_EVENT_TAG_ADDED / NFC_EVENT_TAG_REMOVED
    BD_EVENT_BUTTON
    POWD_EVENT_BATTERY_CRITICAL
    WSM_EVENT_WIFI_CONNECTED / WSM_EVENT_WIFI_LOST
    PLSM_EVENT_AUTHENTICATED / AUTH_FAILED / AUTH_LOST / DISCONNECTED
    PLSM_EVENT_TAG_RESOLVED / TAG_UNKNOWN
    PLSM_EVENT_PLAY_STOPPED

- Outputs:
    Commands to WSM / PLSM
    Events:
        HCSM_EVENT_INITIATED
        HCSM_EVENT_SHUTDOWN
        HCSM_EVENT_STATE_CHANGED

No direct commands to LED daemon; LED logic subscribes to HCSM events.
"""

import os
import sys
import json
import time
import socket
import select
import signal
import logging
from typing import Dict, Any, Optional, Set

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"

WSM_CMD_SOCKET = "/tmp/hearo/wsm.sock"
PLSM_CMD_SOCKET = "/tmp/hearo/plsm.sock"

IPC_SCHEMA_EVENT = "hearo.ipc/event"
IPC_SCHEMA_CMD = "hearo.ipc/cmd"

HCSM_ORIGIN = "hcsm"

# Required daemon-start events for leaving SYS_INIT
REQUIRED_DAEMON_EVENTS = {
    "NFC_EVENT_DAEMON_STARTED": "nfcd",
    "BD_EVENT_DAEMON_STARTED": "bd",
    "LEDD_EVENT_DAEMON_STARTED": "ledd",
    "WSM_EVENT_DAEMON_STARTED": "wsm",
    "PLSM_EVENT_DAEMON_STARTED": "plsm",
    "POWD_EVENT_DAEMON_STARTED": "powd",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def epoch_ms() -> int:
    return int(time.time() * 1000.0)


class EventSender:
    """Fire-and-forget event sender to the shared events socket."""

    def __init__(self, path: str) -> None:
        self.path = path

    def send_event(self, event: str, payload: Dict[str, Any]) -> None:
        env = {
            "schema": IPC_SCHEMA_EVENT,
            "v": 1,
            "id": f"evt-hcsm-{epoch_ms()}",
            "ts": epoch_ms(),
            "event": event,
            "payload": payload or {},
        }
        data = json.dumps(env, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.path)
                s.send(data)
        except OSError as e:
            logging.error("HCSM: failed to send event %s: %s", event, e)


class CommandClient:
    """Minimal one-shot command sender. ACK/RESULT ignored for now."""

    def __init__(self, path: str, origin: str = HCSM_ORIGIN) -> None:
        self.path = path
        self.origin = origin

    def send_cmd(self,
                 cmd_name: str,
                 payload: Optional[Dict[str, Any]] = None,
                 timeout_ms: int = 1000) -> None:
        cmd_id = f"cmd-hcsm-{epoch_ms()}"
        env = {
            "schema": IPC_SCHEMA_CMD,
            "v": 1,
            "id": cmd_id,
            "ts": epoch_ms(),
            "cmd": cmd_name,
            "payload": payload or {},
            "reply": "",          # HCSM does not handle cmd replies
            "origin": self.origin,
            "timeout_ms": timeout_ms,
        }
        data = json.dumps(env, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.path)
                s.send(data)
        except OSError as e:
            logging.error("HCSM: failed to send cmd %s to %s: %s",
                          cmd_name, self.path, e)


class EventServer:
    """
    Datagram event server.

    If started via systemd socket activation (hearo-events.socket),
    reuse inherited fd=3. Otherwise bind to EVENT_SOCKET_PATH.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.sock: Optional[socket.socket] = None

    def start(self) -> None:
        listen_fds = int(os.getenv("LISTEN_FDS", "0"))
        listen_pid = os.getenv("LISTEN_PID")

        if listen_fds > 0 and listen_pid and int(listen_pid) == os.getpid():
            fd = 3
            s = socket.socket(fileno=fd)
            s.setblocking(False)
            self.sock = s
            logging.info("HCSM: using systemd-activated event socket (fd=%d)", fd)
            return

        # Fallback: own socket
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                logging.warning("HCSM: could not unlink existing %s", self.path)

        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(self.path)
        s.setblocking(False)
        self.sock = s
        logging.info("HCSM: listening on %s (self-owned)", self.path)

    def recv(self) -> Optional[bytes]:
        if not self.sock:
            return None
        try:
            data, _ = self.sock.recvfrom(65535)
            return data
        except BlockingIOError:
            return None

    def fileno(self) -> int:
        if not self.sock:
            raise RuntimeError("EventServer not started")
        return self.sock.fileno()

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


# ---------------------------------------------------------------------------
# HCSM core
# ---------------------------------------------------------------------------

class HcsmState:
    SYS_INIT = "SYS_INIT"
    SYS_NO_WIFI = "SYS_NO_WIFI"
    SYS_OFFLINE = "SYS_OFFLINE"
    SYS_READY_PAUSED = "SYS_READY_PAUSED"
    SYS_PLAYING = "SYS_PLAYING"
    SYS_SHUTDOWN = "SYS_SHUTDOWN"
    SYS_ERROR = "SYS_ERROR"


class HcsmDaemon:
    def __init__(self, event_server: EventServer) -> None:
        self.event_server = event_server
        self.sender = EventSender(EVENT_SOCKET_PATH)

        self.wsm = CommandClient(WSM_CMD_SOCKET)
        self.plsm = CommandClient(PLSM_CMD_SOCKET)

        self.state: str = HcsmState.SYS_INIT
        self.running: bool = True

        self.daemons_started: Set[str] = set()
        self.wifi_status_seen: bool = False

        self.initiated_emitted: bool = False
        self.current_tag_uid: Optional[str] = None

    # ----------------- state + events -----------------

    def _emit_state_changed(self) -> None:
        self.sender.send_event("HCSM_EVENT_STATE_CHANGED",
                               {"state": self.state})

    def _transition(self, new_state: str) -> None:
        if new_state == self.state:
            return
        logging.info("HCSM: state %s -> %s", self.state, new_state)
        self.state = new_state
        self._emit_state_changed()

        if self.state == HcsmState.SYS_SHUTDOWN:
            self.sender.send_event("HCSM_EVENT_SHUTDOWN", {})

    def _handle_daemon_started(self, event: str) -> None:
        if event in REQUIRED_DAEMON_EVENTS:
            name = REQUIRED_DAEMON_EVENTS[event]
            if name not in self.daemons_started:
                self.daemons_started.add(name)
                logging.info("HCSM: daemon started: %s", name)

    def _all_daemons_started(self) -> bool:
        return set(REQUIRED_DAEMON_EVENTS.values()).issubset(self.daemons_started)

    # ----------------- main event entry -----------------

    def handle_raw_event(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logging.warning("HCSM: invalid JSON event: %s", e)
            return

        if msg.get("schema") != IPC_SCHEMA_EVENT:
            return

        event = msg.get("event")
        payload = msg.get("payload") or {}
        if not isinstance(event, str):
            return

        self._handle_daemon_started(event)

        if event in ("WSM_EVENT_WIFI_CONNECTED", "WSM_EVENT_WIFI_LOST"):
            self.wifi_status_seen = True

        if self.state == HcsmState.SYS_INIT:
            self._handle_init_event(event, payload)
        elif self.state == HcsmState.SYS_NO_WIFI:
            self._handle_no_wifi_event(event, payload)
        elif self.state == HcsmState.SYS_OFFLINE:
            self._handle_offline_event(event, payload)
        elif self.state == HcsmState.SYS_READY_PAUSED:
            self._handle_ready_paused_event(event, payload)
        elif self.state == HcsmState.SYS_PLAYING:
            self._handle_playing_event(event, payload)
        elif self.state == HcsmState.SYS_SHUTDOWN:
            # ignore everything
            return
        elif self.state == HcsmState.SYS_ERROR:
            self._handle_error_event(event, payload)

    # ----------------- per-state handlers -----------------

    def _handle_init_event(self, event: str, payload: Dict[str, Any]) -> None:
        # discard NFC + button during INIT
        if event in ("NFC_EVENT_TAG_ADDED", "NFC_EVENT_TAG_REMOVED", "BD_EVENT_BUTTON"):
            return

        if self._all_daemons_started() and self.wifi_status_seen:
            self._transition(HcsmState.SYS_NO_WIFI)
            if not self.initiated_emitted:
                self.sender.send_event("HCSM_EVENT_INITIATED", {})
                self.initiated_emitted = True

    def _handle_no_wifi_event(self, event: str, payload: Dict[str, Any]) -> None:
        if event == "WSM_EVENT_WIFI_CONNECTED":
            self._transition(HcsmState.SYS_OFFLINE)
            return

        if event == "POWD_EVENT_BATTERY_CRITICAL":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_SHUTDOWN)
            return

        # ignore NFC tag attempts; optional feedback handled by LED daemon

    def _handle_offline_event(self, event: str, payload: Dict[str, Any]) -> None:
        if event == "PLSM_EVENT_AUTHENTICATED":
            self._transition(HcsmState.SYS_READY_PAUSED)
            return

        if event in ("PLSM_EVENT_AUTH_FAILED", "PLSM_EVENT_AUTH_LOST"):
            return  # stay OFFLINE

        if event == "WSM_EVENT_WIFI_LOST":
            self._transition(HcsmState.SYS_NO_WIFI)
            return

        if event == "POWD_EVENT_BATTERY_CRITICAL":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_SHUTDOWN)
            return

        # ignore NFC tag attempts; optional feedback by LED daemon

    def _handle_ready_paused_event(self, event: str, payload: Dict[str, Any]) -> None:
        if event == "NFC_EVENT_TAG_ADDED":
            uid = payload.get("uid")
            if isinstance(uid, str):
                self.current_tag_uid = uid
                self.plsm.send_cmd("PLSM_COMMAND_PLAY_TAG", {"uid": uid})
            return

        if event == "PLSM_EVENT_TAG_RESOLVED":
            self._transition(HcsmState.SYS_PLAYING)
            return

        if event == "PLSM_EVENT_TAG_UNKNOWN":
            # HCSM does not send LED commands; LEDD can react to this event itself if needed
            return

        if event == "WSM_EVENT_WIFI_LOST":
            self._transition(HcsmState.SYS_NO_WIFI)
            return

        if event in ("PLSM_EVENT_DISCONNECTED",
                     "PLSM_EVENT_AUTH_LOST",
                     "PLSM_EVENT_AUTH_FAILED"):
            self._transition(HcsmState.SYS_OFFLINE)
            return

        if event == "POWD_EVENT_BATTERY_CRITICAL":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_SHUTDOWN)
            return

    def _handle_playing_event(self, event: str, payload: Dict[str, Any]) -> None:
        if event == "NFC_EVENT_TAG_ADDED":
            uid = payload.get("uid")
            if isinstance(uid, str):
                self.current_tag_uid = uid
                self.plsm.send_cmd("PLSM_COMMAND_PLAY_TAG", {"uid": uid})
            return

        if event == "PLSM_EVENT_TAG_UNKNOWN":
            # LEDD can map this to feedback if desired
            return

        if event == "PLSM_EVENT_PLAY_STOPPED":
            self._transition(HcsmState.SYS_READY_PAUSED)
            return

        if event == "NFC_EVENT_TAG_REMOVED":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_READY_PAUSED)
            return

        if event == "BD_EVENT_BUTTON":
            self._handle_button_in_playing(payload)
            return

        if event == "WSM_EVENT_WIFI_LOST":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_NO_WIFI)
            return

        if event == "PLSM_EVENT_DISCONNECTED":
            self._transition(HcsmState.SYS_OFFLINE)
            return

        if event in ("PLSM_EVENT_AUTH_LOST", "PLSM_EVENT_AUTH_FAILED"):
            self._transition(HcsmState.SYS_OFFLINE)
            return

        if event == "POWD_EVENT_BATTERY_CRITICAL":
            self._cmd_stop_playback()
            self._transition(HcsmState.SYS_SHUTDOWN)
            return

    def _handle_error_event(self, event: str, payload: Dict[str, Any]) -> None:
        # Minimal: wait until all daemons are up again, then go back to INIT
        if self._all_daemons_started():
            self._transition(HcsmState.SYS_INIT)

    # ----------------- helpers -----------------

    def _cmd_stop_playback(self) -> None:
        self.plsm.send_cmd("PLSM_COMMAND_STOP", {})

    def _handle_button_in_playing(self, payload: Dict[str, Any]) -> None:
        button = payload.get("button")
        interaction = payload.get("interaction")
        if not isinstance(button, str) or not isinstance(interaction, str):
            return

        if button == "NEXT" and interaction == "SHORT_PRESS":
            self.plsm.send_cmd("PLSM_COMMAND_NEXT", {})
        elif button == "PREV" and interaction == "SHORT_PRESS":
            self.plsm.send_cmd("PLSM_COMMAND_PREVIOUS", {})
        elif button in ("NEXT", "PREV") and interaction in ("LONG_PRESS", "HOLD_TICK"):
            delta_ms = 15000 if button == "NEXT" else -15000
            self.plsm.send_cmd("PLSM_COMMAND_SEEK", {"delta_ms": delta_ms})

    # ----------------- lifecycle -----------------

    def setup(self) -> None:
        self.event_server.start()
        logging.info("HCSM: start in SYS_INIT")
        # Ask WSM for status; response will arrive as events
        self.wsm.send_cmd("WSM_COMMAND_STATUS", {})
        self._emit_state_changed()

    def run(self) -> None:
        while self.running:
            try:
                rlist, _, _ = select.select([self.event_server.fileno()], [], [], 0.5)
            except (OSError, ValueError):
                rlist = []

            if rlist:
                raw = self.event_server.recv()
                if raw:
                    self.handle_raw_event(raw)

    def stop(self) -> None:
        self.running = False
        self.event_server.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] HCSM: %(message)s",
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HEARO Central State Machine (HCSM)")
    parser.add_argument(
        "--verbose", action="store_true", help="enable debug logging"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    evsrv = EventServer(EVENT_SOCKET_PATH)
    hcsm = HcsmDaemon(evsrv)

    def _handle_signal(signum, frame):
        logging.info("HCSM: signal %s, stopping", signum)
        hcsm.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    hcsm.setup()
    hcsm.run()


if __name__ == "__main__":
    main()
