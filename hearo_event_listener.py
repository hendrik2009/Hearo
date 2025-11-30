#!/usr/bin/env python3
import socket, json, os

EVENT_SOCKET = "/tmp/hearo/events.sock"

if os.path.exists(EVENT_SOCKET):
    os.unlink(EVENT_SOCKET)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
sock.bind(EVENT_SOCKET)
print("Listening on", EVENT_SOCKET)

while True:
    data, _ = sock.recvfrom(8192)
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception as e:
        print("INVALID:", e, data)
    else:
        print(json.dumps(msg, indent=2))
