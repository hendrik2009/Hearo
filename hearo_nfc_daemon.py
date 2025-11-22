#!/usr/bin/env python3
"""
Hearo NFC Reader Daemon (fixed timeouts & debug)
Emits JSON events to a Unix datagram socket and optional CLI logs.

Usage examples:
  python3 hearo_nfc_daemon.py -v --scan-debug --socket-path /tmp/hearo_events.sock
  sudo python3 hearo_nfc_daemon.py -v
"""

import json, os, time, socket, sys, signal, argparse
from typing import Optional

import board
import busio
from adafruit_pn532.i2c import PN532_I2C

# ---------------- Configuration (defaults; can be overridden by CLI) -------------
SOCKET_PATH = "/tmp/hearo/events.sock"
HEARTBEAT_ENABLED = True
HEARTBEAT_PERIOD_S = 1.0
DEBOUNCE_MS = 300
MISS_RELEASE_MS = 600
READ_INTERVAL_MS = 50            # faster poll
RETRY_READS = 10                 # more attempts per cycle
RETRY_WINDOW_MS = 1000           # longer window
# ---------------------------------------------------------------------------------

def now_ms() -> int:
    return int(time.time() * 1000)

def send_event(sock, ev_type: str, payload: dict, socket_path: str = SOCKET_PATH):
    msg = {"ts": time.time(), "type": ev_type, "payload": payload}
    data = (json.dumps(msg) + "\n").encode("utf-8")
    try:
        sock.sendto(data, socket_path)
    except Exception:
        pass
    try:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()
    except Exception:
        pass

def ensure_socket_dir():
    d = os.path.dirname(SOCKET_PATH)
    if not os.path.exists(d):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            # e.g. /run/hearo when running unprivileged; ignore
            pass
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except Exception:
        pass

def open_client_socket():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.setblocking(False)
    return s

class NFCReader:
    def __init__(self):
        i2c = busio.I2C(board.SCL, board.SDA)
        self.pn532 = PN532_I2C(i2c, debug=False)
        self.pn532.SAM_configuration()

    def read_uid_once(self) -> Optional[str]:
        # Longer per-read timeout so we actually catch tags reliably
        uid = self.pn532.read_passive_target(timeout=0.2)
        if uid is None:
            return None
        return "".join(f"{b:02X}" for b in uid)

def main():
    parser = argparse.ArgumentParser(description="Hearo NFC Reader Daemon")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print human-readable messages")
    parser.add_argument("--socket-path", default=SOCKET_PATH,
                        help="Unix datagram socket path")
    parser.add_argument("--scan-debug", action="store_true",
                        help="print UID whenever a scan returns one")
    args = parser.parse_args()

    verbose = args.verbose
    scan_debug = args.scan_debug
    socket_path = args.socket_path  # use a local variable instead of global

    ensure_socket_dir()
    sock = open_client_socket()
    reader = NFCReader()

    current_uid: Optional[str] = None
    candidate_uid: Optional[str] = None
    candidate_since_ms = 0
    last_seen_ms = 0
    last_heartbeat_ms = 0
    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _stop)

    send_event(sock, "nfc_daemon_started", {"version": 1})
    if verbose:
        print("[INFO] NFC daemon started")

    while running:
        start_cycle = now_ms()
        uid = None

        # Retry window to resist brief read gaps
        deadline = start_cycle + RETRY_WINDOW_MS
        attempts = 0
        while attempts < RETRY_READS and now_ms() < deadline:
            attempts += 1
            uid = reader.read_uid_once()
            if uid:
                if scan_debug:
                    print(f"[SCAN] UID {uid}")
                break
            time.sleep(0.01)

        t_ms = now_ms()

        if uid:
            if current_uid is None:
                if candidate_uid != uid:
                    candidate_uid = uid
                    candidate_since_ms = t_ms
                else:
                    if (t_ms - candidate_since_ms) >= DEBOUNCE_MS:
                        current_uid = uid
                        last_seen_ms = t_ms
                        send_event(sock, "tag_added", {"uid": current_uid})
                        if verbose:
                            print(f"[ADD] Tag {current_uid}")
            else:
                if uid == current_uid:
                    last_seen_ms = t_ms
                else:
                    send_event(sock, "tag_removed", {"uid": current_uid})
                    if verbose:
                        print(f"[REMOVE] Tag {current_uid} (replaced)")
                    current_uid = None
                    candidate_uid = uid
                    candidate_since_ms = t_ms
        else:
            if current_uid and (t_ms - last_seen_ms) >= MISS_RELEASE_MS:
                send_event(sock, "tag_removed", {"uid": current_uid})
                if verbose:
                    print(f"[REMOVE] Tag {current_uid} (timeout)")
                current_uid = None
                candidate_uid = None
                candidate_since_ms = 0

        if HEARTBEAT_ENABLED and current_uid:
            if (t_ms - last_heartbeat_ms) >= int(HEARTBEAT_PERIOD_S * 1000):
                last_heartbeat_ms = t_ms
                send_event(sock, "tag_present", {"uid": current_uid})
                if verbose:
                    print(f"[PRESENT] Tag {current_uid}")

        elapsed = now_ms() - start_cycle
        time.sleep(max(0, READ_INTERVAL_MS - elapsed) / 1000.0)

    send_event(sock, "nfc_daemon_stopped", {})
    if verbose:
        print("[INFO] NFC daemon stopped")

if __name__ == "__main__":
    main()
