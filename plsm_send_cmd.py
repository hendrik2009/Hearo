#!/usr/bin/env python3
import socket, json, time

CMD_SOCKET = "/tmp/hearo/psm_cmd.sock"
EVENT_SOCKET = "/tmp/hearo/events.sock"

def send_play_tag(uid: str):
    cmd = {
        "schema": "hearo.ipc/cmd",
        "v": 1,
        "id": f"cmd-{int(time.time()*1000)}",
        "ts": int(time.time()*1000),
        "cmd": "PLSM_COMMAND_PLAY_TAG",
        "payload": {"uid": uid},
        "reply": EVENT_SOCKET,
        "timeout_ms": 1000,
    }
    data = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.connect(CMD_SOCKET)
    s.send(data)
    s.close()

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: plsm_send_cmd.py <UID>")
        sys.exit(1)
    send_play_tag(sys.argv[1])
