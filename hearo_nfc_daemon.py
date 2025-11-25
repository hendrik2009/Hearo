#!/usr/bin/env python3
"""
HEARO NFC Daemon (NFCD) – Spec-compliant implementation

- PN532 via I2C
- Unix datagram IPC:
    * Events  -> /tmp/hearo/events.sock
    * Commands-> /tmp/hearo/nfcd.sock
- Implements:
    * NFC_EVENT_DAEMON_STARTED
    * NFC_EVENT_READY
    * NFC_EVENT_TAG_ADDED
    * NFC_EVENT_TAG_PRESENT
    * NFC_EVENT_TAG_REMOVED {reason: timeout|replaced}
    * NFC_EVENT_ERROR
    * NFC_EVENT_DAEMON_STOPPED
- Commands:
    * NFC_CMD_PING
    * NFC_CMD_SET_DEBUG {level}
    * NFC_CMD_RESTART
- Debounce / timing as per NFCD spec.
"""

import os
import sys
import json
import time
import socket
import signal
import logging
import argparse
from typing import Optional, Dict, Any

import board
import busio
from adafruit_pn532.i2c import PN532_I2C

# ---------------------------------------------------------------------------
# Config (defaults match spec)
# ---------------------------------------------------------------------------

EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"
CMD_SOCKET_PATH = "/tmp/hearo/nfcd.sock"

READ_INTERVAL_MS = 50
RETRY_READS = 10
RETRY_WINDOW_MS = 1000
DEBOUNCE_MS = 300
MISS_RELEASE_MS = 600
TAG_HEARTBEAT_PERIOD_MS = 1000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        "id": f"evt-nfcd-{_evt_counter}",
        "ts": epoch_ms(),
        "event": event,
        "payload": payload,
    }


def make_ack_envelope(cmd_id: str, ok: bool, error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _ack_counter
    _ack_counter += 1
    return {
        "schema": "hearo.ipc/ack",
        "v": 1,
        "id": f"ack-nfcd-{_ack_counter}",
        "ts": epoch_ms(),
        "in-reply-to": cmd_id,
        "ok": bool(ok),
        "error": error if not ok else None,
    }


def make_result_envelope(cmd_id: str, ok: bool, payload: Dict[str, Any],
                         error: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    global _res_counter
    _res_counter += 1
    return {
        "schema": "hearo.ipc/result",
        "v": 1,
        "id": f"res-nfcd-{_res_counter}",
        "ts": epoch_ms(),
        "in-reply-to": cmd_id,
        "ok": bool(ok),
        "payload": payload,
        "error": error if not ok else None,
    }


class EventSender:
    def __init__(self, path: str) -> None:
        self.path = path

    def send(self, env: Dict[str, Any]) -> None:
        data = json.dumps(env, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(self.path)
                s.send(data)
        except OSError as e:
            # spec: should not crash on IPC issues
            logging.warning("NFCD: failed to send event to %s: %s", self.path, e)


# ---------------------------------------------------------------------------
# NFC Reader wrapper
# ---------------------------------------------------------------------------

class NFCReader:
    def __init__(self) -> None:
        self.i2c = None
        self.pn532 = None

    def init_hw(self) -> None:
        """Initialise PN532; emit HW_NOT_FOUND / PN532_RESET_FAILED via caller on error."""
        self.i2c = busio.I2C(board.SCL, board.SDA)
        self.pn532 = PN532_I2C(self.i2c, debug=False)
        # SAM configuration
        self.pn532.SAM_configuration()
        logging.info("NFCD: PN532 initialised")

    def read_uid_once(self) -> Optional[str]:
        uid = self.pn532.read_passive_target(timeout=0.2)
        if uid is None:
            return None
        return "".join(f"{b:02X}" for b in uid)  # uppercase hex, no separators


# ---------------------------------------------------------------------------
# Command server
# ---------------------------------------------------------------------------

class CommandServer:
    """
    Unix datagram command listener on /tmp/hearo/nfcd.sock.
    Expects hearo.ipc/cmd with NFC_CMD_*.
    """

    def __init__(self, path: str, sender: EventSender, daemon_ctx: Dict[str, Any]) -> None:
        self.path = path
        self.sender = sender
        self.daemon_ctx = daemon_ctx

        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.error("NFCD: cannot unlink old cmd socket %s: %s", self.path, e)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.setblocking(False)

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

    def poll_once(self) -> None:
        try:
            data, _ = self.sock.recvfrom(4096)
        except BlockingIOError:
            return
        except OSError as e:
            logging.error("NFCD: error reading cmd socket: %s", e)
            return

        try:
            msg = json.loads(data.decode("utf-8"))
        except Exception as e:
            logging.warning("NFCD: invalid JSON on cmd socket: %s", e)
            return

        if msg.get("schema") != "hearo.ipc/cmd":
            logging.warning("NFCD: ignoring non-cmd schema: %r", msg.get("schema"))
            return

        cmd_id = msg.get("id") or "cmd-unknown"
        cmd = msg.get("cmd")
        payload = msg.get("payload") or {}
        reply = msg.get("reply")

        if not reply:
            logging.warning("NFCD: command without reply path (id=%s)", cmd_id)
            return

        if cmd == "NFC_CMD_PING":
            self._handle_ping(cmd_id, reply)
        elif cmd == "NFC_CMD_SET_DEBUG":
            self._handle_set_debug(cmd_id, reply, payload)
        elif cmd == "NFC_CMD_RESTART":
            self._handle_restart(cmd_id, reply)
        else:
            self._handle_unknown(cmd_id, reply, cmd)

    def _send_ipc(self, path: str, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.connect(path)
                s.send(data)
        except OSError as e:
            logging.error("NFCD: failed to send cmd response to %s: %s", path, e)

    def _handle_ping(self, cmd_id: str, reply: str) -> None:
        ack = make_ack_envelope(cmd_id, ok=True)
        self._send_ipc(reply, ack)

        uptime_ms = int((time.monotonic() - self.daemon_ctx["t_start"]) * 1000)
        result_payload = {
            "status": self.daemon_ctx.get("status", "init"),
            "current_uid": self.daemon_ctx.get("current_uid"),
            "last_error_code": self.daemon_ctx.get("last_error_code"),
            "uptime_ms": uptime_ms,
            "poll_interval_ms": READ_INTERVAL_MS,
            "tag_heartbeat_period_ms": TAG_HEARTBEAT_PERIOD_MS,
        }
        res = make_result_envelope(cmd_id, ok=True, payload=result_payload)
        self._send_ipc(reply, res)

    def _handle_set_debug(self, cmd_id: str, reply: str, payload: Dict[str, Any]) -> None:
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
        error = None
        if ok:
            logging.getLogger().setLevel(level)
            logging.info("NFCD: log level set to %s", level_str)
        else:
            error = {"code": "INVALID_LEVEL", "message": f"invalid level '{level_str}'"}

        ack = make_ack_envelope(cmd_id, ok=ok, error=error)
        self._send_ipc(reply, ack)

        res_payload = {"applied_level": level_str if ok else None}
        res = make_result_envelope(cmd_id, ok=ok, payload=res_payload, error=error)
        self._send_ipc(reply, res)

    def _handle_restart(self, cmd_id: str, reply: str) -> None:
        # Mark restart request in context; daemon will re-init PN532
        self.daemon_ctx["restart_requested"] = True
        ack = make_ack_envelope(cmd_id, ok=True)
        self._send_ipc(reply, ack)
        res = make_result_envelope(cmd_id, ok=True, payload={"restart": "scheduled"})
        self._send_ipc(reply, res)

    def _handle_unknown(self, cmd_id: str, reply: str, cmd: Any) -> None:
        err = {"code": "UNKNOWN_CMD", "message": f"unknown command '{cmd}'"}
        ack = make_ack_envelope(cmd_id, ok=False, error=err)
        self._send_ipc(reply, ack)
        res = make_result_envelope(cmd_id, ok=False, payload={}, error=err)
        self._send_ipc(reply, res)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class NFCDaemon:
    STATE_INIT = "NFC_STATE_INIT"
    STATE_READY = "NFC_STATE_READY"
    STATE_ERROR = "NFC_STATE_ERROR"

    def __init__(self, verbose: bool = False, scan_debug: bool = False):
        self.verbose = verbose
        self.scan_debug = scan_debug

        self.sender = EventSender(EVENT_SOCKET_PATH)
        self.ctx: Dict[str, Any] = {
            "status": "init",
            "current_uid": None,
            "last_error_code": None,
            "t_start": time.monotonic(),
            "restart_requested": False,
        }

        self.reader = NFCReader()
        self.cmd_server: Optional[CommandServer] = None

        self.state = self.STATE_INIT

        # Tag tracking
        self.current_uid: Optional[str] = None
        self.candidate_uid: Optional[str] = None
        self.candidate_since_ms: int = 0
        self.last_seen_ms: int = 0
        self.last_heartbeat_ms: int = 0

        self.running = True

    # --- IPC events ---------------------------------------------------------

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        env = make_event_envelope(event, payload)
        self.sender.send(env)

    def _emit_error(self, code: str, message: str, recovering: bool) -> None:
        self.ctx["status"] = "error"
        self.ctx["last_error_code"] = code
        payload = {"code": code, "message": message, "recovering": bool(recovering)}
        self._emit("NFC_EVENT_ERROR", payload)
        logging.error("NFCD: error %s (recovering=%s): %s", code, recovering, message)

    # --- Setup / teardown ---------------------------------------------------

    def setup(self) -> None:
        # Command socket
        self.cmd_server = CommandServer(CMD_SOCKET_PATH, self.sender, self.ctx)

        # Emit DAEMON_STARTED
        started_payload = {"version": 1, "pid": os.getpid()}
        self._emit("NFC_EVENT_DAEMON_STARTED", started_payload)
        logging.info("NFCD: daemon started")

        # Init PN532
        self._init_pn532()

    def _init_pn532(self) -> None:
        try:
            self.reader.init_hw()
        except Exception as e:
            self.state = self.STATE_ERROR
            self._emit_error("HW_NOT_FOUND", f"PN532 init failed: {e}", recovering=True)
            return

        self.state = self.STATE_READY
        self.ctx["status"] = "ready"
        self._emit("NFC_EVENT_READY", {})
        logging.info("NFCD: state READY")

    def teardown(self, reason: str) -> None:
        logging.info("NFCD: shutting down (%s)", reason)
        try:
            self._emit("NFC_EVENT_DAEMON_STOPPED", {"reason": reason, "pid": os.getpid()})
        except Exception:
            pass

        if self.cmd_server is not None:
            self.cmd_server.close()

    # --- Main loop ----------------------------------------------------------

    def run(self) -> None:
        import select

        self.setup()

        def handle_signal(signum, frame):
            logging.info("NFCD: received signal %s, stopping", signum)
            self.running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        try:
            while self.running:
                cycle_start_ms = epoch_ms()

                # Commands
                if self.cmd_server is not None:
                    r, _, _ = select.select([self.cmd_server.fileno()], [], [], 0.0)
                    if r:
                        self.cmd_server.poll_once()

                # Restart request
                if self.ctx.get("restart_requested"):
                    logging.info("NFCD: restart requested, reinitialising PN532")
                    self.ctx["restart_requested"] = False
                    self._init_pn532()

                # NFC behaviour only in READY; in ERROR we can periodically retry
                if self.state == self.STATE_READY:
                    self._poll_nfc()
                elif self.state == self.STATE_ERROR:
                    # crude recovery attempt every few seconds
                    if cycle_start_ms % 5000 < READ_INTERVAL_MS:
                        logging.info("NFCD: attempting recovery from ERROR")
                        self._init_pn532()

                elapsed = epoch_ms() - cycle_start_ms
                remaining = READ_INTERVAL_MS - elapsed
                if remaining > 0:
                    time.sleep(remaining / 1000.0)

        except Exception as e:
            self._emit_error("INTERNAL_EXCEPTION", str(e), recovering=False)
            raise
        finally:
            self.teardown("signal" if not self.running else "exception")

    # --- NFC logic ----------------------------------------------------------

    def _poll_nfc(self) -> None:
        start_ms = epoch_ms()
        uid = None

        deadline = start_ms + RETRY_WINDOW_MS
        attempts = 0

        while attempts < RETRY_READS and epoch_ms() < deadline:
            attempts += 1
            try:
                uid = self.reader.read_uid_once()
            except Exception as e:
                self._emit_error("I2C_TIMEOUT", f"read failed: {e}", recovering=True)
                self.state = self.STATE_ERROR
                return

            if uid:
                if self.scan_debug:
                    logging.debug("NFCD: SCAN UID %s", uid)
                break
            time.sleep(0.01)

        now = epoch_ms()

        # Tag detection and debounce
        if uid:
            if self.current_uid is None:
                if self.candidate_uid != uid:
                    self.candidate_uid = uid
                    self.candidate_since_ms = now
                else:
                    if (now - self.candidate_since_ms) >= DEBOUNCE_MS:
                        self.current_uid = uid
                        self.ctx["current_uid"] = uid
                        self.last_seen_ms = now
                        self._emit(
                            "NFC_EVENT_TAG_ADDED",
                            {"uid": uid, "tech": "ISO14443", "ats": None},
                        )
                        if self.verbose:
                            logging.info("NFCD: TAG_ADDED %s", uid)
            else:
                if uid == self.current_uid:
                    self.last_seen_ms = now
                else:
                    # replaced
                    old_uid = self.current_uid
                    self._emit(
                        "NFC_EVENT_TAG_REMOVED",
                        {"uid": old_uid, "reason": "replaced"},
                    )
                    if self.verbose:
                        logging.info("NFCD: TAG_REMOVED %s (replaced)", old_uid)
                    self.current_uid = None
                    self.candidate_uid = uid
                    self.candidate_since_ms = now
        else:
            # no uid; check timeout
            if self.current_uid and (now - self.last_seen_ms) >= MISS_RELEASE_MS:
                old_uid = self.current_uid
                self._emit(
                    "NFC_EVENT_TAG_REMOVED",
                    {"uid": old_uid, "reason": "timeout"},
                )
                if self.verbose:
                    logging.info("NFCD: TAG_REMOVED %s (timeout)", old_uid)
                self.current_uid = None
                self.ctx["current_uid"] = None
                self.candidate_uid = None
                self.candidate_since_ms = 0

        # Heartbeat
        if self.current_uid:
            if (now - self.last_heartbeat_ms) >= TAG_HEARTBEAT_PERIOD_MS:
                self.last_heartbeat_ms = now
                self._emit("NFC_EVENT_TAG_PRESENT", {"uid": self.current_uid})
                if self.verbose:
                    logging.debug("NFCD: TAG_PRESENT %s", self.current_uid)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def configure_logging(verbose: bool) -> None:
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] nfcd: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="HEARO NFC Daemon (NFCD)")
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    parser.add_argument("--scan-debug", action="store_true", help="log every UID read")
    args = parser.parse_args()

    configure_logging(args.verbose)

    daemon = NFCDaemon(verbose=args.verbose, scan_debug=args.scan_debug)
    daemon.run()


if __name__ == "__main__":
    main()
