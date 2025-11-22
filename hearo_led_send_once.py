#!/usr/bin/env python3
import socket, json, time

CMD_SOCKET = "/tmp/hearo/ledd.sock"

msg = {
    "schema": "hearo.ipc/cmd",
    "v": 1,
    "id": "test-1",
    "ts": int(time.time()*1000),
    "cmd": "LED_SET_STATE",
    "payload": {
        "mode": "steady",
        "color": {"r": 0, "g": 255, "b": 0},
        "brightness": 255
    },
    "reply": "/tmp/hearo/ignore.sock"
}

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(CMD_SOCKET)
s.sendall(json.dumps(msg).encode("utf-8"))
s.close()
print("sent")
