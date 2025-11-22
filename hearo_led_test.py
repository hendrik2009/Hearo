#!/usr/bin/env python3
import socket, json, time, os, sys

CMD_SOCKET = "/tmp/hearo/ledd.sock"
REPLY_SOCKET = "/tmp/hearo/led_test_reply.sock"


def send_cmd(cmd, payload=None, msg_id=None):
    if payload is None:
        payload = {}
    if msg_id is None:
        msg_id = f"test-{int(time.time()*1000)}"

    # ensure reply dir exists
    os.makedirs(os.path.dirname(REPLY_SOCKET), exist_ok=True)

    # prepare reply socket listener
    if os.path.exists(REPLY_SOCKET):
        os.unlink(REPLY_SOCKET)
    rep = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    rep.bind(REPLY_SOCKET)
    rep.listen(1)

    msg = {
        "schema": "hearo.ipc/cmd",
        "v": 1,
        "id": msg_id,
        "ts": int(time.time() * 1000),
        "cmd": cmd,
        "payload": payload,
        "reply": REPLY_SOCKET,
    }

    # send
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(CMD_SOCKET)
    c.sendall(json.dumps(msg).encode("utf-8"))
    c.close()

    # read single response (ACK or RESULT)
    conn, _ = rep.accept()
    resp = conn.recv(4096).decode()
    conn.close()
    rep.close()
    print("RESPONSE:", resp)


def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  hearo_led_test.py steady")
        print("  hearo_led_test.py wave")
        print("  hearo_led_test.py feedback")
        print("  hearo_led_test.py error_on")
        print("  hearo_led_test.py error_off")
        print("  hearo_led_test.py off")
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "steady":
        send_cmd("LED_SET_STATE", {
            "mode": "steady",
            "color": {"r": 0, "g": 255, "b": 0},
            "brightness": 255
        })

    elif mode == "wave":
        send_cmd("LED_SET_STATE", {
            "mode": "wave",
            "color": {"r": 0, "g": 0, "b": 255},
            "brightness": 255,
            "shape": "smooth",
            "period_ms": 1500,
        })

    elif mode == "feedback":
        send_cmd("LED_SET_FEEDBACK", {
            "mode": "wave",
            "color": {"r": 255, "g": 0, "b": 0},
            "brightness": 50,
            "shape": "square",
            "period_ms": 500,
            "duration_ms": 2000
        })

    elif mode == "error_on":
        send_cmd("LED_SET_ERROR", {"enabled": True})

    elif mode == "error_off":
        send_cmd("LED_SET_ERROR", {"enabled": False})

    elif mode == "off":
        send_cmd("LED_OFF", {})

    else:
        print("Unknown mode:", mode)
        sys.exit(1)


if __name__ == "__main__":
    main()
