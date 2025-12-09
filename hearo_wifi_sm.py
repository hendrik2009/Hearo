#!/usr/bin/env python3
"""
HEARO WiFi State Machine (WSM) Daemon – Spec-Oriented Implementation

- Manages Wi-Fi connectivity and AP mode.
- Talks IPC over Unix datagram sockets:
    * Commands  <- /tmp/hearo/wsm.sock
    * Events    -> /tmp/hearo/events.sock

Implements:
    * States: WSM_INIT, WSM_APMODE, WSM_CONNECTED, WSM_ERROR
    * Events:
        - WSM_EVENT_WIFI_CONNECTED
        - WSM_EVENT_WIFI_LOST
        - WSM_EVENT_WIFI_AP_STARTED
        - WSM_EVENT_WIFI_AP_STOPPED
    * Command:
        - WSM_COMMAND_STATUS
    * ACK / RESULT per "HEARO – IPC Message Scheme (V1.3)"
"""

import argparse
import json
import logging
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# IPC paths (can be made configurable via CLI if needed)
CMD_SOCKET_PATH = "/tmp/hearo/wsm.sock"
EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"

# Wi-Fi / network tools (adjust if your system uses different services)
WLAN_IFACE = "wlan0"
WPA_CLI = ["wpa_cli", "-i", WLAN_IFACE]
PING_SPOTIFY = ["ping", "-c", "1", "-W", "2", "api.spotify.com"]

# AP control – these are intentionally generic wrappers.
# Adapt the commands to your concrete systemd service names.
AP_START_CMD = ["systemctl", "start", "hearo-ap.target"]
AP_STOP_CMD = ["systemctl", "stop", "hearo-ap.target"]

# Connectivity / retry parameters
CONNECTIVITY_CHECK_INTERVAL_S = 10
STATION_STATUS_REFRESH_S = 5
RETRY_INITIAL_DELAY_S = 5
RETRY_MAX_DELAY_S = 60

# Helpers ---------------------------------------------------------------------


def epoch_ms() -> int:
    return int(time.time() * 1000)


def safe_run(cmd: Any, timeout: int = 5) -> Tuple[int, str, str]:
    """
    Run a shell command safely, returning (rc, stdout, stderr).
    On failure, returns rc != 0 and empty strings.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=True,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)

def send_event(event, payload=None):
        env = {
            "schema": "hearo.ipc/event",
            "v": 1,
            "id": f"evt-wsm-{int(time.time()*1000)}",
            "ts": int(time.time()*1000),
            "event": event,
            "payload": payload or {},
        }
        data = json.dumps(env, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect("/tmp/hearo/events.sock")
                s.send(data)
        except OSError as e:
            logging.warning("WSM: failed to send event %s: %s", event, e)

# State / status models -------------------------------------------------------


@dataclass
class APStatus:
    active: bool = False
    ssid: Optional[str] = None
    clients: Optional[int] = None


@dataclass
class StationStatus:
    connected: bool = False
    ssid: Optional[str] = None
    ip: Optional[str] = None
    rssi: Optional[int] = None


@dataclass
class InternetStatus:
    spotify_reachable: bool = False
    last_check_ms_ago: int = 0
    fail_streak: int = 0
    last_check_ts: int = field(default_factory=epoch_ms)


@dataclass
class WiFiStatus:
    state: str = "WSM_INIT"
    ap: APStatus = field(default_factory=APStatus)
    station: StationStatus = field(default_factory=StationStatus)
    internet: InternetStatus = field(default_factory=InternetStatus)
    uptime_ms: int = 0
    last_error_code: Optional[str] = None


# WSM implementation ----------------------------------------------------------


class WiFiStateMachine:
    def __init__(self, event_socket_path: str, logger: logging.Logger):
        self.event_socket_path = event_socket_path
        self.log = logger

        self.status = WiFiStatus()
        self.start_ts = epoch_ms()

        # timers / backoff
        self._next_station_check = 0.0
        self._next_connectivity_check = 0.0
        self._retry_delay = RETRY_INITIAL_DELAY_S

        # cached AP config (static for now; could be made dynamic)
        self.ap_ssid = "Hearo-Setup"
        self.ap_channel = 6
        self.ap_security = "WPA2-PSK"

    # --- IPC helpers -----------------------------------------------------

    def send_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        msg = {
            "schema": "hearo.ipc/event",
            "v": 1,
            "id": f"evt-wsm-{epoch_ms()}",
            "ts": epoch_ms(),
            "event": event_type,
            "payload": payload or {},
        }
        data = json.dumps(msg).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.event_socket_path)
                s.send(data)
        except Exception as e:
            self.log.error("Failed to send event %s: %s", event_type, e)

    def send_ack(
        self, cmd: Dict[str, Any], ok: bool, error_code: Optional[str] = None, error_msg: Optional[str] = None
    ) -> None:
        reply = cmd.get("reply")
        if not reply:
            return
        ack = {
            "schema": "hearo.ipc/ack",
            "v": 1,
            "id": f"ack-wsm-{epoch_ms()}",
            "ts": epoch_ms(),
            "in-reply-to": cmd.get("id"),
            "ok": ok,
            "error": None,
        }
        if not ok:
            ack["error"] = {"code": error_code or "ERR_BAD_CMD", "message": error_msg or "Command rejected"}
        data = json.dumps(ack).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(reply)
                s.send(data)
        except Exception as e:
            self.log.error("Failed to send ACK to %s: %s", reply, e)

    def send_result(
        self,
        cmd: Dict[str, Any],
        ok: bool,
        payload: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        error_msg: Optional[str] = None,
    ) -> None:
        reply = cmd.get("reply")
        if not reply:
            return
        res = {
            "schema": "hearo.ipc/result",
            "v": 1,
            "id": f"res-wsm-{epoch_ms()}",
            "ts": epoch_ms(),
            "corr": cmd.get("id"),
            "ok": ok,
            "payload": payload or {},
            "error": None,
        }
        if not ok:
            res["error"] = {"code": error_code or "ERR_EXEC", "message": error_msg or "Execution error"}
        data = json.dumps(res).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(reply)
                s.send(data)
        except Exception as e:
            self.log.error("Failed to send RESULT to %s: %s", reply, e)

    # --- Wi-Fi / AP helpers ----------------------------------------------

    def _update_station_status(self) -> None:
        """
        Update self.status.station from OS tools.
        This is intentionally conservative and robust against missing tools.
        """
        st = self.status.station

        # SSID via wpa_cli or iwgetid
        ssid = None
        rc, out, _ = safe_run(WPA_CLI + ["status"])
        if rc == 0 and out:
            for line in out.splitlines():
                if line.startswith("ssid="):
                    ssid = line.split("=", 1)[1].strip() or None
        if not ssid:
            rc, out, _ = safe_run(["iwgetid", "-r"])
            if rc == 0 and out:
                ssid = out.strip() or None

        # IP via hostname -I
        ip = None
        rc, out, _ = safe_run(["hostname", "-I"])
        if rc == 0 and out:
            parts = out.split()
            ip = parts[0] if parts else None

        # RSSI via iw dev
        rssi = None
        rc, out, _ = safe_run(["iw", "dev", WLAN_IFACE, "link"])
        if rc == 0 and out:
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("signal:"):
                    # e.g. "signal: -52 dBm"
                    try:
                        rssi = int(line.split()[1])
                    except Exception:
                        pass

        st.ssid = ssid
        st.ip = ip
        st.rssi = rssi
        st.connected = bool(ssid and ip)

    def _check_spotify_connectivity(self) -> None:
        now = epoch_ms()
        rc, _out, _err = safe_run(PING_SPOTIFY)
        internet = self.status.internet
        if rc == 0:
            internet.spotify_reachable = True
            internet.fail_streak = 0
        else:
            internet.spotify_reachable = False
            internet.fail_streak += 1
        internet.last_check_ms_ago = 0
        internet.last_check_ts = now

    def _update_internet_age(self) -> None:
        now = epoch_ms()
        internet = self.status.internet
        internet.last_check_ms_ago = max(0, now - internet.last_check_ts)

    def _start_ap(self) -> None:
        if self.status.ap.active:
            return
        rc, _out, err = safe_run(AP_START_CMD)
        if rc != 0:
            self.log.error("Failed to start AP: %s", err)
            self.status.last_error_code = "ERR_AP_START"
            return
        self.status.ap.active = True
        self.status.ap.ssid = self.ap_ssid
        # Client count will be updated by other diagnostics if needed.
        self.send_event(
            "WSM_EVENT_WIFI_AP_STARTED",
            {"ssid": self.ap_ssid, "channel": self.ap_channel, "security": self.ap_security},
        )

    def _stop_ap(self, reason: str = "station_connected") -> None:
        if not self.status.ap.active:
            return
        rc, _out, err = safe_run(AP_STOP_CMD)
        if rc != 0:
            self.log.error("Failed to stop AP: %s", err)
            self.status.last_error_code = "ERR_AP_STOP"
        self.status.ap.active = False
        self.send_event("WSM_EVENT_WIFI_AP_STOPPED", {"reason": reason})

    def _ensure_wifi_stack_available(self) -> bool:
        # Minimal sanity checks; can be extended.
        rc, _out, _err = safe_run(["which", "wpa_cli"])
        if rc != 0:
            self.log.error("wpa_cli not available")
            self.status.last_error_code = "ERR_NO_WPA_CLI"
            return False
        return True

    # --- State machine ----------------------------------------------------

    def _transition(self, new_state: str) -> None:
        if new_state == self.status.state:
            return
        self.log.info("WSM state: %s -> %s", self.status.state, new_state)
        self.status.state = new_state
        # Reset retry timer when entering APMODE
        if new_state == "WSM_APMODE":
            self._retry_delay = RETRY_INITIAL_DELAY_S
            self._next_station_check = 0.0

    def handle_init(self, now: float) -> None:
        # Check Wi-Fi stack; decide next state.
        if self._ensure_wifi_stack_available():
            self._transition("WSM_APMODE")
        else:
            self._transition("WSM_ERROR")

    def handle_apmode(self, now: float) -> None:
        # Ensure AP is up.
        if not self.status.ap.active:
            self._start_ap()

        # Periodically refresh station status and try to establish connectivity.
        if now >= self._next_station_check:
            self._update_station_status()
            # Light-touch "attempt": ask wpa_supplicant to reconnect if not connected.
            if not self.status.station.connected:
                _rc, _out, _err = safe_run(WPA_CLI + ["reconnect"])
            # If station looks connected, run connectivity check.
            if self.status.station.connected:
                self._check_spotify_connectivity()

            # Backoff update
            self._retry_delay = min(
                self._retry_delay * 2 if self._retry_delay > 0 else RETRY_INITIAL_DELAY_S,
                RETRY_MAX_DELAY_S,
            )
            self._next_station_check = now + self._retry_delay

        # Transition logic
        if self.status.station.connected and self.status.internet.spotify_reachable:
            self.send_event(
                "WSM_EVENT_WIFI_CONNECTED",
                {
                    "ssid": self.status.station.ssid,
                    "ip": self.status.station.ip,
                    "rssi": self.status.station.rssi,
                },
            )
            self._stop_ap(reason="station_connected")
            self._transition("WSM_CONNECTED")

    def handle_connected(self, now: float) -> None:
        # Monitor station + internet regularly
        if now >= self._next_station_check:
            self._update_station_status()
            self._next_station_check = now + STATION_STATUS_REFRESH_S

        if now >= self._next_connectivity_check:
            self._check_spotify_connectivity()
            self._next_connectivity_check = now + CONNECTIVITY_CHECK_INTERVAL_S

        # Determine if we've effectively "lost" connectivity
        self._update_internet_age()
        lost = False
        reason = None

        if not self.status.station.connected:
            lost = True
            reason = "link_down"
        elif not self.status.station.ip:
            lost = True
            reason = "no_ip"
        elif not self.status.internet.spotify_reachable:
            lost = True
            reason = "no_internet"

        if lost:
            self.send_event(
                "WSM_EVENT_WIFI_LOST",
                {
                    "reason": reason,
                    "ssid": self.status.station.ssid,
                    "ip": self.status.station.ip,
                    "fail_streak": self.status.internet.fail_streak,
                },
            )
            # Go back to AP mode and start recovery
            self._transition("WSM_APMODE")

    def handle_error(self, now: float) -> None:
        # Simple self-recovery attempt: periodically re-check Wi-Fi stack.
        if now >= self._next_station_check:
            if self._ensure_wifi_stack_available():
                self._transition("WSM_APMODE")
            else:
                # Try again later, with capped backoff
                self._retry_delay = min(
                    self._retry_delay * 2 if self._retry_delay else RETRY_INITIAL_DELAY_S,
                    RETRY_MAX_DELAY_S,
                )
                self._next_station_check = now + self._retry_delay

    def tick(self) -> None:
        """
        Single state-machine step; call regularly from main loop.
        """
        now = time.time()
        self.status.uptime_ms = epoch_ms() - self.start_ts

        if self.status.state == "WSM_INIT":
            self.handle_init(now)
        elif self.status.state == "WSM_APMODE":
            self.handle_apmode(now)
        elif self.status.state == "WSM_CONNECTED":
            self.handle_connected(now)
        elif self.status.state == "WSM_ERROR":
            self.handle_error(now)
        else:
            self.log.error("Unknown state %s", self.status.state)
            self.status.state = "WSM_ERROR"

    # --- Command handling -------------------------------------------------

    def handle_command(self, raw: bytes) -> None:
        try:
            cmd = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self.log.error("Failed to decode command: %s", e)
            return

        if cmd.get("schema") != "hearo.ipc/cmd":
            self.log.warning("Ignoring non-command message: %s", cmd.get("schema"))
            return

        command_type = cmd.get("cmd")
        if command_type != "WSM_COMMAND_STATUS":
            # Unknown/malformed command -> ACK with error, RESULT with error
            self.send_ack(cmd, ok=False, error_code="ERR_UNKNOWN_CMD", error_msg=f"Unknown command {command_type}")
            self.send_result(
                cmd, ok=False, error_code="ERR_UNKNOWN_CMD", error_msg=f"Unknown command {command_type}"
            )
            return

        # WSM_COMMAND_STATUS has empty payload
        self.send_ack(cmd, ok=True)

        payload = {
            "state": self.status.state,
            "ap_mode": {
                "active": self.status.ap.active,
                "ssid": self.status.ap.ssid,
                "clients": self.status.ap.clients,
            },
            "station": {
                "connected": self.status.station.connected,
                "ssid": self.status.station.ssid,
                "ip": self.status.station.ip,
                "rssi": self.status.station.rssi,
            },
            "internet": {
                "spotify_reachable": self.status.internet.spotify_reachable,
                "last_check_ms_ago": self.status.internet.last_check_ms_ago,
                "fail_streak": self.status.internet.fail_streak,
            },
            "uptime_ms": self.status.uptime_ms,
            "last_error_code": self.status.last_error_code,
        }
        self.send_result(cmd, ok=True, payload=payload)


# Daemon main loop ------------------------------------------------------------


class WSMDaemon:
    def __init__(self, cmd_socket_path: str, event_socket_path: str, log_level: str = "INFO"):
        self.log = logging.getLogger("WSM")
        self.log.setLevel(getattr(logging, log_level.upper(), logging.INFO))
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
        self.log.addHandler(handler)

        self.cmd_socket_path = cmd_socket_path
        self.event_socket_path = event_socket_path

        self.sel = selectors.DefaultSelector()
        self.cmd_sock: Optional[socket.socket] = None

        self.wsm = WiFiStateMachine(event_socket_path=self.event_socket_path, logger=self.log)
        self.running = True

    def setup_sockets(self) -> None:
        # Command socket
        if os.path.exists(self.cmd_socket_path):
            os.unlink(self.cmd_socket_path)
        cmd_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        cmd_sock.bind(self.cmd_socket_path)
        self.cmd_sock = cmd_sock
        self.sel.register(cmd_sock, selectors.EVENT_READ, self.on_cmd_ready)
        self.log.info("WSM command socket bound on %s", self.cmd_socket_path)

    def on_cmd_ready(self, sock: socket.socket) -> None:
        try:
            data, addr = sock.recvfrom(65535)
        except Exception as e:
            self.log.error("Error reading command socket: %s", e)
            return
        self.wsm.handle_command(data)

    def run(self) -> None:
        self.setup_sockets()

        def handle_signal(signum, frame):
            self.log.info("Received signal %s, shutting down", signum)
            self.running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self.log.info("WSM daemon started; entering main loop")
        send_event("WSM_EVENT_DAEMON_STARTED", {})

        # Initial state is WSM_INIT; tick will transition appropriately.
        while self.running:
            # State machine step
            self.wsm.tick()

            # Wait for commands with a short timeout
            events = self.sel.select(timeout=0.5)
            for key, _mask in events:
                callback = key.data
                callback(key.fileobj)

        # Cleanup
        if self.cmd_sock:
            self.sel.unregister(self.cmd_sock)
            self.cmd_sock.close()
        try:
            if os.path.exists(self.cmd_socket_path):
                os.unlink(self.cmd_socket_path)
        except Exception:
            pass
        self.log.info("WSM daemon stopped")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HEARO WiFi State Machine (WSM) daemon")
    parser.add_argument(
        "--cmd-sock",
        default=CMD_SOCKET_PATH,
        help=f"Command socket path (default: {CMD_SOCKET_PATH})",
    )
    parser.add_argument(
        "--event-sock",
        default=EVENT_SOCKET_PATH,
        help=f"Event socket path (default: {EVENT_SOCKET_PATH})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )

    args = parser.parse_args(argv)

    daemon = WSMDaemon(cmd_socket_path=args.cmd_sock, event_socket_path=args.event_sock, log_level=args.log_level)
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
