#!/usr/bin/env python3
# Minimaler Hearo-Event-Daemon: empfängt Button-Events (UNIX-DGRAM), print + Log
import os, socket, json, select, logging, logging.handlers, time
from typing import Any, Dict

SOCKET_PATH = "/tmp/hearo_buttons.sock"          # muss zum Sender passen
LOG_PATH    = "/home/hendrik/hearo/logs/events.log"
PRINTS_ON   = True                                   # Konsole an/aus
SOCKET_MODE = 0o666                                  # ggf. 0o660 und Gruppe nutzen

def setup_logging(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)

def log_console(msg: str) -> None:
    if PRINTS_ON:
        print(msg, flush=True)

def ensure_socket(path: str) -> socket.socket:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    os.chmod(path, SOCKET_MODE)
    return s

def pretty(evt: Dict[str, Any]) -> str:
    # kurze, gut lesbare Darstellung
    t = evt.get("type", "unknown")
    btn = evt.get("button", "")
    extra = {k: v for k, v in evt.items() if k not in ("type", "button", "ts")}
    return f"{t}{' - ' + btn if btn else ''}{' ' + str(extra) if extra else ''}"

def main():
    setup_logging(LOG_PATH)
    sock = ensure_socket(SOCKET_PATH)
    log_console(f"Listening on {SOCKET_PATH} (Ctrl+C to exit)")
    logging.info("Daemon started, socket ready")

    try:
        while True:
            r, _, _ = select.select([sock], [], [], 1.0)
            if not r:
                continue
            try:
                data, _ = sock.recvfrom(4096)
                msg = data.decode("utf-8", errors="replace").strip()
                # JSON bevorzugt, aber handle auch Plaintext
                try:
                    evt = json.loads(msg)
                except json.JSONDecodeError:
                    evt = {"type": "raw", "payload": msg, "ts": time.time()}

                text = pretty(evt)
                log_console(f"RX: {text}")
                logging.info("RX %s", evt)
            except Exception as e:
                log_console(f"(warn) recv error: {e}")
                logging.warning("recv error: %s", e)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sock.close()
        finally:
            try:
                if os.path.exists(SOCKET_PATH):
                    os.unlink(SOCKET_PATH)
            except OSError:
                pass
        logging.info("Daemon stopped")

if __name__ == "__main__":
    main()