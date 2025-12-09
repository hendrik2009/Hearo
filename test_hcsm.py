#!/usr/bin/env python3
"""
test_hcsm.py – minimal HCSM exerciser

Usage examples (in another shell while HCSM runs):

  python3 test_hcsm.py init-sequence
  python3 test_hcsm.py wifi-up
  python3 test_hcsm.py auth-ok
  python3 test_hcsm.py tag-play 04AABBCCDD01
  python3 test_hcsm.py play-stopped
  python3 test_hcsm.py wifi-lost
  python3 test_hcsm.py bat-critical
"""

import os
import sys
import json
import time
import socket
import argparse
from typing import Dict, Any

EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"
IPC_SCHEMA_EVENT = "hearo.ipc/event"


def epoch_ms() -> int:
    return int(time.time() * 1000.0)


def send_event(event: str, payload: Dict[str, Any]) -> None:
    env = {
        "schema": IPC_SCHEMA_EVENT,
        "v": 1,
        "id": f"evt-test-{epoch_ms()}",
        "ts": epoch_ms(),
        "event": event,
        "payload": payload or {},
    }
    data = json.dumps(env, separators=(",", ":")).encode("utf-8")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(EVENT_SOCKET_PATH)
        sock.send(data)
    finally:
        sock.close()
    print(f"sent {event} {payload}")


def cmd_init_sequence() -> None:
    """
    Drive HCSM out of SYS_INIT:
    - all DAEMON_STARTED
    - one WiFi status event
    """
    daemon_events = [
        "NFC_EVENT_DAEMON_STARTED",
        "BD_EVENT_DAEMON_STARTED",
        "LEDD_EVENT_DAEMON_STARTED",
        "WSM_EVENT_DAEMON_STARTED",
        "PLSM_EVENT_DAEMON_STARTED",
        "POWD_EVENT_DAEMON_STARTED",
    ]
    for ev in daemon_events:
        send_event(ev, {})
        time.sleep(0.05)

    # one WiFi status event -> should trigger SYS_NO_WIFI
    send_event("WSM_EVENT_WIFI_LOST", {})


def cmd_wifi_up() -> None:
    send_event("WSM_EVENT_WIFI_CONNECTED", {})


def cmd_auth_ok() -> None:
    send_event("PLSM_EVENT_AUTHENTICATED", {})


def cmd_tag_play(uid: str) -> None:
    send_event("NFC_EVENT_TAG_ADDED", {"uid": uid})
    # Simulate successful resolution
    time.sleep(0.1)
    send_event("PLSM_EVENT_TAG_RESOLVED", {"uid": uid})


def cmd_play_stopped() -> None:
    send_event("PLSM_EVENT_PLAY_STOPPED", {})


def cmd_wifi_lost() -> None:
    send_event("WSM_EVENT_WIFI_LOST", {})


def cmd_battery_critical() -> None:
    send_event("POWD_EVENT_BATTERY_CRITICAL", {})


def main() -> None:
    parser = argparse.ArgumentParser(description="HCSM test driver")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-sequence")
    sub.add_parser("wifi-up")
    sub.add_parser("auth-ok")

    p_tag = sub.add_parser("tag-play")
    p_tag.add_argument("uid", help="tag UID to simulate")

    sub.add_parser("play-stopped")
    sub.add_parser("wifi-lost")
    sub.add_parser("bat-critical")

    args = parser.parse_args()

    if args.cmd == "init-sequence":
        cmd_init_sequence()
    elif args.cmd == "wifi-up":
        cmd_wifi_up()
    elif args.cmd == "auth-ok":
        cmd_auth_ok()
    elif args.cmd == "tag-play":
        cmd_tag_play(args.uid)
    elif args.cmd == "play-stopped":
        cmd_play_stopped()
    elif args.cmd == "wifi-lost":
        cmd_wifi_lost()
    elif args.cmd == "bat-critical":
        cmd_battery_critical()
    else:
        parser.error("unknown command")


if __name__ == "__main__":
    main()
