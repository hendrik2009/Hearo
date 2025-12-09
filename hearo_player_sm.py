#!/usr/bin/env python3
"""
HEARO Player State Machine (PLSM) Daemon – Web API implementation

Implements:
- States: PL_INIT, PL_AUTHENTICATING, PL_READY, PL_PLAYING, PL_ERROR
- Auth substates: AUTH_NONE, AUTH_PENDING, AUTH_OK, AUTH_FAILED, AUTH_LOST

Commands (IPC, Unix datagram):
    * PLSM_COMMAND_PLAY_TAG {uid}
    * PLSM_COMMAND_STOP {}
    * PLSM_COMMAND_NEXT {}
    * PLSM_COMMAND_PREVIOUS {}
    * PLSM_COMMAND_SEEK {delta_ms}
    * PLSM_COMMAND_PLAY {uri, position_ms}
    * PLSM_COMMAND_SHUTDOWN {}

Events:
    * PLSM_EVENT_AUTHENTICATED {}
    * PLSM_EVENT_AUTH_FAILED {reason}
    * PLSM_EVENT_AUTH_LOST {reason}
    * PLSM_EVENT_DISCONNECTED {}
    * PLSM_EVENT_TAG_RESOLVED {uid, uri, position_ms}
    * PLSM_EVENT_TAG_UNKNOWN {uid}
    * PLSM_EVENT_PLAY_STARTED {uid, uri}
    * PLSM_EVENT_PLAY_STOPPED {}
    * PLSM_EVENT_STATE_CHANGED {old, new}
    * PLSM_EVENT_PLAYBACK_ERROR {code, message}   (optional)

DB (minimal tag mapping schema):
    /var/lib/hearo/hearo.db
    table tags:
        uid TEXT PRIMARY KEY
        playlist_uri   TEXT NOT NULL
        last_track_uri TEXT NOT NULL DEFAULT ''
        last_pos_ms    INTEGER NOT NULL DEFAULT 0
        updated_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))

Spotify/Web API:
    Token file: /var/lib/hearo/spotify_token.json
    {
      "access_token": "...",
      "refresh_token": "...",
      "client_id": "...",
      "client_secret": "..."
    }

    Playback controlled via:
      - GET  /v1/me/player/devices
      - GET  /v1/me/player
      - PUT  /v1/me/player/play
      - PUT  /v1/me/player/pause
      - PUT  /v1/me/player/seek
      - POST /v1/me/player/next
      - POST /v1/me/player/previous
"""

import os
import sys
import time
import json
import sqlite3
import socket
import signal
import logging
from typing import Optional, Dict, Any, Tuple

from urllib import request, parse, error as urlerror

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

DB_PATH = "/var/lib/hearo/hearo.db"

CMD_SOCKET_PATH = "/tmp/hearo/psm_cmd.sock"
EVENT_SOCKET_PATH = "/tmp/hearo/events.sock"

SPOTIFY_TOKEN_FILE = "/var/lib/hearo/spotify_token.json"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_ENDPOINT = "https://accounts.spotify.com/api/token"

# librespot device name substring (case-insensitive)
LIBRESPOT_DEVICE_NAME = "Hearo"

IPC_SCHEMA_CMD = "hearo.ipc/cmd"
IPC_SCHEMA_EVENT = "hearo.ipc/event"
IPC_SCHEMA_ACK = "hearo.ipc/ack"
IPC_SCHEMA_RESULT = "hearo.ipc/result"  # not used

PL_STATE_INIT = "PL_INIT"
PL_STATE_AUTHENTICATING = "PL_AUTHENTICATING"
PL_STATE_READY = "PL_READY"
PL_STATE_PLAYING = "PL_PLAYING"
PL_STATE_ERROR = "PL_ERROR"

AUTH_NONE = "AUTH_NONE"
AUTH_PENDING = "AUTH_PENDING"
AUTH_OK = "AUTH_OK"
AUTH_FAILED = "AUTH_FAILED"
AUTH_LOST = "AUTH_LOST"

PROGRESS_SAVE_INTERVAL_MS = 2000  # progress persistence interval
MAIN_LOOP_TICK_MS = 200           # main loop tick

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def epoch_ms() -> int:
    return int(time.time() * 1000.0)


# ---------------------------------------------------------------------------
# Backend: Spotify Web API controlling librespot
# ---------------------------------------------------------------------------

class BackendError(Exception):
    def __init__(self, message: str, code: str = "BACKEND_ERROR", auth_issue: bool = False,
                 device_issue: bool = False):
        super().__init__(message)
        self.code = code
        self.auth_issue = auth_issue
        self.device_issue = device_issue


class BackendStatus:
    def __init__(self, is_playing: bool, uri: Optional[str], position_ms: int):
        self.is_playing = is_playing
        self.uri = uri
        self.position_ms = position_ms


class WebAPIBackend:
    """
    Spotify Web API backend for controlling a single librespot Connect device.

    Responsibilities:
    - Manage access_token and refresh_token.
    - Discover LIBRESPOT device_id by name (LIBRESPOT_DEVICE_NAME).
    - Implement play/pause/seek/next/previous/stop and get_status.
    """

    def __init__(self, token_file: str, device_name_substr: str) -> None:
        self.token_file = token_file
        self.device_name_substr = (device_name_substr or "").lower()
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.client_id: Optional[str] = None
        self.client_secret: Optional[str] = None
        self.device_id: Optional[str] = None

    # -------- token and device management --------

    def load_token_file(self) -> None:
        if not os.path.isfile(self.token_file):
            raise BackendError(
                f"token file not found: {self.token_file}",
                code="NO_CREDENTIALS",
                auth_issue=True,
            )
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise BackendError(
                f"failed to read token file: {e}",
                code="NO_CREDENTIALS",
                auth_issue=True,
            )

        self.access_token = data.get("access_token")
        self.refresh_token = data.get("refresh_token")
        self.client_id = data.get("client_id")
        self.client_secret = data.get("client_secret")

        if not self.access_token and not self.refresh_token:
            raise BackendError(
                "no usable tokens in token file",
                code="NO_CREDENTIALS",
                auth_issue=True,
            )

    def save_access_token(self) -> None:
        """
        Persist updated access_token back into token file.
        """
        try:
            with open(self.token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if self.access_token:
            data["access_token"] = self.access_token
        try:
            with open(self.token_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            logging.warning("PLSM backend: failed to save updated access_token: %s", e)

    def _http(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes],
              timeout: float = 10.0) -> Tuple[int, str]:
        req = request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
                resp_body = resp.read().decode("utf-8", errors="replace")
                return status, resp_body
        except urlerror.HTTPError as e:
            resp_body = e.read().decode("utf-8", errors="replace")
            return e.code, resp_body
        except Exception as e:
            raise BackendError(f"HTTP error: {e}", code="NETWORK_ERROR")

    def _api_request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                     body: Optional[Dict[str, Any]] = None,
                     retry_on_auth: bool = True) -> Tuple[int, str]:
        if not self.access_token:
            # try refresh if possible
            self.refresh_access_token()

        if not self.access_token:
            raise BackendError("no access_token available", code="AUTH_REQUIRED", auth_issue=True)

        url = SPOTIFY_API_BASE + path
        if params:
            url += "?" + parse.urlencode(params)

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else None

        status, resp_body = self._http(method, url, headers, body_bytes)

        if status in (401, 403) and retry_on_auth:
            # auth problem: try refresh once
            logging.warning("PLSM backend: got %s, attempting token refresh", status)
            self.refresh_access_token()
            if not self.access_token:
                raise BackendError("auth failed after refresh", code="AUTH_FAILED", auth_issue=True)
            headers["Authorization"] = f"Bearer {self.access_token}"
            status, resp_body = self._http(method, url, headers, body_bytes)

        if status in (401, 403):
            raise BackendError(
                f"Spotify auth error {status}: {resp_body}",
                code="AUTH_FAILED",
                auth_issue=True,
            )

        return status, resp_body

    def refresh_access_token(self) -> None:
        if not self.refresh_token or not self.client_id or not self.client_secret:
            # no refresh possible
            logging.error("PLSM backend: cannot refresh token (missing refresh_token/client_id/secret)")
            self.access_token = None
            raise BackendError("no refresh credentials", code="NO_CREDENTIALS", auth_issue=True)

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        body = parse.urlencode(data).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        status, resp_body = self._http("POST", SPOTIFY_TOKEN_ENDPOINT, headers, body)
        if status != 200:
            raise BackendError(
                f"token refresh failed ({status}): {resp_body}",
                code="AUTH_FAILED",
                auth_issue=True,
            )

        try:
            j = json.loads(resp_body)
        except Exception as e:
            raise BackendError(
                f"invalid refresh response: {e}",
                code="AUTH_FAILED",
                auth_issue=True,
            )

        self.access_token = j.get("access_token")
        if not self.access_token:
            raise BackendError("no access_token in refresh response", auth_issue=True)

        # Spotify may or may not send a new refresh_token
        new_rt = j.get("refresh_token")
        if new_rt:
            self.refresh_token = new_rt

        self.save_access_token()
        logging.info("PLSM backend: access_token refreshed")

    def discover_device(self) -> None:
        """
        Discover the librespot device_id via /me/player/devices.
        Only devices whose name contains device_name_substr are accepted.
        """
        status, resp_body = self._api_request("GET", "/me/player/devices")
        if status != 200:
            raise BackendError(
                f"/me/player/devices failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

        try:
            j = json.loads(resp_body)
        except Exception as e:
            raise BackendError(f"invalid devices JSON: {e}", code="BACKEND_ERROR")

        devices = j.get("devices") or []
        name_sub = self.device_name_substr
        found_id: Optional[str] = None
        for d in devices:
            name = (d.get("name") or "").lower()
            if not name:
                continue
            if name_sub and name_sub in name:
                found_id = d.get("id")
                break

        if not found_id:
            raise BackendError(
                f"no device matching name substring '{name_sub}'",
                code="DEVICE_UNAVAILABLE",
                device_issue=True,
            )

        self.device_id = found_id
        logging.info("PLSM backend: using device_id %s", self.device_id)

    def ensure_ready(self) -> None:
        """
        Ensure we have token + device_id and can talk to backend.
        """
        self.load_token_file()
        # Try a lightweight device discovery; this will refresh token if needed
        self.discover_device()

    # -------- public playback API --------

    def play(self, uri: str, position_ms: int) -> None:
        if not self.device_id:
            self.ensure_ready()
        body: Dict[str, Any]
        if uri.startswith("spotify:track:"):
            body = {"uris": [uri]}
        else:
            body = {"context_uri": uri}
        if position_ms and position_ms > 0:
            body["position_ms"] = int(position_ms)

        params = {"device_id": self.device_id}
        status, resp_body = self._api_request("PUT", "/me/player/play", params=params, body=body)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/play failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def stop(self) -> None:
        if not self.device_id:
            self.ensure_ready()
        params = {"device_id": self.device_id}
        status, resp_body = self._api_request("PUT", "/me/player/pause", params=params, body=None)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/pause failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def pause(self) -> None:
        self.stop()

    def resume(self) -> None:
        # Resume current context; no URI needed
        if not self.device_id:
            self.ensure_ready()
        params = {"device_id": self.device_id}
        status, resp_body = self._api_request("PUT", "/me/player/play", params=params, body=None)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/play (resume) failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def seek_abs(self, position_ms: int) -> None:
        if not self.device_id:
            self.ensure_ready()
        pos = max(0, int(position_ms))
        params = {
            "device_id": self.device_id,
            "position_ms": pos,
        }
        status, resp_body = self._api_request("PUT", "/me/player/seek", params=params, body=None)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/seek failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def next(self) -> None:
        if not self.device_id:
            self.ensure_ready()
        params = {"device_id": self.device_id}
        status, resp_body = self._api_request("POST", "/me/player/next", params=params, body=None)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/next failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def previous(self) -> None:
        if not self.device_id:
            self.ensure_ready()
        params = {"device_id": self.device_id}
        status, resp_body = self._api_request("POST", "/me/player/previous", params=params, body=None)
        if status not in (200, 202, 204):
            raise BackendError(
                f"/me/player/previous failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )

    def get_status(self) -> BackendStatus:
        status, resp_body = self._api_request("GET", "/me/player")
        if status == 204:
            # No active device / playback
            return BackendStatus(False, None, 0)
        if status != 200:
            raise BackendError(
                f"/me/player failed ({status}): {resp_body}",
                code="BACKEND_ERROR",
            )
        try:
            j = json.loads(resp_body)
        except Exception as e:
            raise BackendError(
                f"invalid /me/player JSON: {e}",
                code="BACKEND_ERROR",
            )

        is_playing = bool(j.get("is_playing"))
        position_ms = int(j.get("progress_ms") or 0)
        item = j.get("item") or {}
        uri = item.get("uri")
        return BackendStatus(is_playing, uri, position_ms)


# ---------------------------------------------------------------------------
# DB access (SQL Tag Mapping Store — Minimal Schema)
# ---------------------------------------------------------------------------

class TagStore:
    """
    Wrapper around the minimal `tags` table schema.

    SELECT uid, playlist_uri, last_track_uri, last_pos_ms FROM tags WHERE uid=?
    UPDATE tags SET last_track_uri=?, last_pos_ms=?, updated_at=strftime('%s','now') WHERE uid=?
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def resolve_tag(self, uid: str) -> Optional[Dict[str, Any]]:
        if self.conn is None:
            raise RuntimeError("DB not open")
        cur = self.conn.cursor()
        cur.execute(
            "SELECT uid, playlist_uri, last_track_uri, last_pos_ms FROM tags WHERE uid=?",
            (uid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "uid": row["uid"],
            "playlist_uri": row["playlist_uri"],
            "last_track_uri": row["last_track_uri"],
            "last_pos_ms": int(row["last_pos_ms"]),
        }

    def update_progress(self, uid: str, current_uri: str, position_ms: int) -> None:
        if self.conn is None:
            raise RuntimeError("DB not open")
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE tags
            SET last_track_uri = ?,
                last_pos_ms    = ?,
                updated_at     = strftime('%s','now')
            WHERE uid = ?
            """,
            (current_uri, int(position_ms), uid),
        )
        self.conn.commit()


# ---------------------------------------------------------------------------
# IPC helpers
# ---------------------------------------------------------------------------

class EventSender:
    def __init__(self, path: str) -> None:
        self.path = path

    def send_event(self, event: str, payload: Dict[str, Any]) -> None:
        env = {
            "schema": IPC_SCHEMA_EVENT,
            "v": 1,
            "id": f"evt-plsm-{epoch_ms()}",
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
            logging.warning("PLSM: failed to send event %s: %s", event, e)


def send_ack(cmd: Dict[str, Any], ok: bool, error_code: Optional[str] = None,
             error_message: Optional[str] = None) -> None:
    reply = cmd.get("reply")
    if not reply:
        return
    env: Dict[str, Any] = {
        "schema": IPC_SCHEMA_ACK,
        "v": 1,
        "id": f"ack-plsm-{epoch_ms()}",
        "ts": epoch_ms(),
        "in-reply-to": cmd.get("id"),
        "ok": bool(ok),
        "error": None,
    }
    if not ok:
        env["error"] = {
            "code": error_code or "ERROR",
            "message": error_message or "",
        }
    data = json.dumps(env, separators=(",", ":")).encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(reply)
            s.send(data)
    except OSError as e:
        logging.warning("PLSM: failed to send ACK: %s", e)


# ---------------------------------------------------------------------------
# PLSM core
# ---------------------------------------------------------------------------

class PLSMDaemon:
    def __init__(self) -> None:
        self.state = PL_STATE_INIT
        self.auth_state = AUTH_NONE

        self.sender = EventSender(EVENT_SOCKET_PATH)
        self.db = TagStore(DB_PATH)
        self.backend = WebAPIBackend(SPOTIFY_TOKEN_FILE, LIBRESPOT_DEVICE_NAME)

        self.running = True

        # current session info
        self.current_uid: Optional[str] = None
        self.current_playlist_uri: Optional[str] = None
        self.current_track_uri: Optional[str] = None
        self.current_position_ms: int = 0

        self.last_progress_save_ms: int = epoch_ms()

        self._setup_complete = False

    # --------------- lifecycle -------------------

    def setup(self) -> None:
        # Open DB
        try:
            self.db.open()
        except Exception as e:
            logging.error("PLSM: failed to open DB: %s", e)
            self._transition_state(PL_STATE_ERROR)
            return

        # Auth + backend readiness
        self._transition_state(PL_STATE_AUTHENTICATING)
        self.auth_state = AUTH_PENDING

        try:
            self.backend.ensure_ready()
        except BackendError as e:
            if e.auth_issue:
                self._emit_auth_failed(f"startup_auth_failed:{e.code}")
                self._transition_state(PL_STATE_ERROR)
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_unavailable:{e.code}")
                self._transition_state(PL_STATE_ERROR)
            else:
                self._emit_playback_error(e.code, str(e))
                self._transition_state(PL_STATE_ERROR)
            return

        # Ready
        self.auth_state = AUTH_OK
        self.sender.send_event("PLSM_EVENT_AUTHENTICATED", {})
        self._transition_state(PL_STATE_READY)

        self._setup_complete = True

    def teardown(self) -> None:
        self.db.close()

    def _transition_state(self, new_state: str) -> None:
        if new_state == self.state:
            return
        old = self.state
        self.state = new_state
        self.sender.send_event(
            "PLSM_EVENT_STATE_CHANGED",
            {"old": old, "new": new_state}
        )
        logging.info("PLSM: state %s -> %s", old, new_state)

    # --------------- event helpers ----------------

    def _emit_auth_failed(self, reason: str) -> None:
        if self.auth_state != AUTH_FAILED:
            self.auth_state = AUTH_FAILED
            self.sender.send_event(
                "PLSM_EVENT_AUTH_FAILED",
                {"reason": reason}
            )

    def _emit_auth_lost(self, reason: str) -> None:
        if self.auth_state != AUTH_LOST:
            self.auth_state = AUTH_LOST
            self.sender.send_event(
                "PLSM_EVENT_AUTH_LOST",
                {"reason": reason}
            )

    def _emit_playback_error(self, code: str, message: str) -> None:
        self.sender.send_event(
            "PLSM_EVENT_PLAYBACK_ERROR",
            {"code": code, "message": message}
        )

    # --------------- tag resolution ----------------

    def _resolve_tag(self, uid: str) -> Optional[Tuple[str, int]]:
        """
        Resolve UID -> (uri, position_ms) according to minimal schema.

        Policy:
        - If last_track_uri != "" and last_pos_ms > 0: resume there.
        - Else: start at playlist_uri from 0.
        """
        info = self.db.resolve_tag(uid)
        if info is None:
            return None
        playlist_uri = info["playlist_uri"]
        last_track_uri = info["last_track_uri"] or ""
        last_pos_ms = info["last_pos_ms"]
        if last_track_uri and last_pos_ms > 0:
            uri = last_track_uri
            pos = max(0, int(last_pos_ms))
        else:
            uri = playlist_uri
            pos = 0
        self.sender.send_event(
            "PLSM_EVENT_TAG_RESOLVED",
            {"uid": uid, "uri": uri, "position_ms": pos}
        )
        return uri, pos

    # --------------- progress persistence ----------

    def _persist_progress(self) -> None:
        """
        Persist current playback position for current tag.
        """
        if self.current_uid is None or self.current_track_uri is None:
            return
        try:
            status = self.backend.get_status()
        except BackendError as e:
            logging.error("PLSM: get_status failed in progress persistence: %s", e)
            if e.auth_issue:
                self._emit_auth_lost(f"status_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            self._emit_playback_error(e.code, str(e))
            return

        position_ms = status.position_ms
        try:
            self.db.update_progress(self.current_uid, self.current_track_uri, position_ms)
        except Exception as e:
            logging.error("PLSM: failed to persist progress: %s", e)
            self._emit_playback_error("SQL_ERROR", str(e))

    # --------------- playback control --------------

    def _start_playback(self, uid: Optional[str], uri: str, position_ms: int) -> None:
        try:
            self.backend.play(uri, position_ms)
        except BackendError as e:
            logging.error("PLSM: backend play failed: %s", e)
            if e.auth_issue:
                self._emit_auth_lost(f"play_auth_issue:{e.code}")
                self._transition_state(PL_STATE_ERROR)
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
                self._transition_state(PL_STATE_ERROR)
            else:
                self._emit_playback_error(e.code, str(e))
                self._transition_state(PL_STATE_ERROR)
            return

        self.current_uid = uid
        self.current_playlist_uri = None
        self.current_track_uri = uri
        self.current_position_ms = position_ms
        self.last_progress_save_ms = epoch_ms()

        self.sender.send_event(
            "PLSM_EVENT_PLAY_STARTED",
            {"uid": uid, "uri": uri}
        )
        self._transition_state(PL_STATE_PLAYING)

    def _stop_playback(self) -> None:
        if self.state != PL_STATE_PLAYING:
            return
        self._persist_progress()
        try:
            self.backend.stop()
        except BackendError as e:
            logging.error("PLSM: backend stop failed: %s", e)
            if e.auth_issue:
                self._emit_auth_lost(f"stop_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            self._emit_playback_error(e.code, str(e))
        self.sender.send_event("PLSM_EVENT_PLAY_STOPPED", {})
        self._transition_state(PL_STATE_READY)

    # --------------- auth helper -------------------

    def _require_auth_ok(self, cmd: Dict[str, Any]) -> bool:
        if self.auth_state != AUTH_OK:
            self._emit_auth_failed("auth_not_ok")
            send_ack(cmd, False, "AUTH_REQUIRED", "Authentication not OK")
            return False
        return True

    # --------------- command handling --------------

    def handle_command(self, cmd: Dict[str, Any]) -> None:
        if cmd.get("schema") != IPC_SCHEMA_CMD:
            logging.warning("PLSM: ignoring non-cmd schema %r", cmd.get("schema"))
            return
        name = cmd.get("cmd")
        payload = cmd.get("payload") or {}

        logging.info("PLSM: command %s", name)

        if name == "PLSM_COMMAND_PLAY_TAG":
            self._cmd_play_tag(cmd, payload)
        elif name == "PLSM_COMMAND_STOP":
            self._cmd_stop(cmd)
        elif name == "PLSM_COMMAND_NEXT":
            self._cmd_next(cmd)
        elif name == "PLSM_COMMAND_PREVIOUS":
            self._cmd_previous(cmd)
        elif name == "PLSM_COMMAND_SEEK":
            self._cmd_seek(cmd, payload)
        elif name == "PLSM_COMMAND_PLAY":
            self._cmd_play(cmd, payload)
        elif name == "PLSM_COMMAND_SHUTDOWN":
            self._cmd_shutdown(cmd)
        else:
            send_ack(cmd, False, "UNKNOWN_COMMAND", f"Unknown command {name}")

    def _cmd_play_tag(self, cmd: Dict[str, Any], payload: Dict[str, Any]) -> None:
        uid = payload.get("uid")
        if not isinstance(uid, str) or not uid:
            send_ack(cmd, False, "BAD_PAYLOAD", "Missing uid")
            return

        if not self._require_auth_ok(cmd):
            return

        try:
            resolved = self._resolve_tag(uid)
        except Exception as e:
            logging.error("PLSM: SQL error in PLAY_TAG: %s", e)
            self._emit_playback_error("SQL_ERROR", str(e))
            send_ack(cmd, False, "SQL_ERROR", "Tag lookup failed")
            return

        if resolved is None:
            self.sender.send_event("PLSM_EVENT_TAG_UNKNOWN", {"uid": uid})
            send_ack(cmd, False, "TAG_UNMAPPED", "Tag not in DB")
            return

        uri, position_ms = resolved

        # Hot-swap: persist current before switching to new tag
        if self.state == PL_STATE_PLAYING and self.current_uid != uid:
            self._persist_progress()

        self._start_playback(uid, uri, position_ms)
        send_ack(cmd, True)

    def _cmd_stop(self, cmd: Dict[str, Any]) -> None:
        if self.state == PL_STATE_PLAYING:
            self._stop_playback()
        send_ack(cmd, True)

    def _cmd_next(self, cmd: Dict[str, Any]) -> None:
        if self.state != PL_STATE_PLAYING or not self._require_auth_ok(cmd):
            if self.state != PL_STATE_PLAYING:
                send_ack(cmd, False, "NO_ACTIVE_PLAYBACK", "No active playback")
            return
        try:
            self.backend.next()
        except BackendError as e:
            self._emit_playback_error(e.code, str(e))
            if e.auth_issue:
                self._emit_auth_lost(f"next_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            send_ack(cmd, False, e.code, "next failed")
            return
        send_ack(cmd, True)

    def _cmd_previous(self, cmd: Dict[str, Any]) -> None:
        if self.state != PL_STATE_PLAYING or not self._require_auth_ok(cmd):
            if self.state != PL_STATE_PLAYING:
                send_ack(cmd, False, "NO_ACTIVE_PLAYBACK", "No active playback")
            return
        try:
            self.backend.previous()
        except BackendError as e:
            self._emit_playback_error(e.code, str(e))
            if e.auth_issue:
                self._emit_auth_lost(f"previous_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            send_ack(cmd, False, e.code, "previous failed")
            return
        send_ack(cmd, True)

    def _cmd_seek(self, cmd: Dict[str, Any], payload: Dict[str, Any]) -> None:
        if self.state != PL_STATE_PLAYING or not self._require_auth_ok(cmd):
            if self.state != PL_STATE_PLAYING:
                send_ack(cmd, False, "NO_ACTIVE_PLAYBACK", "No active playback")
            return
        delta_ms = payload.get("delta_ms")
        if not isinstance(delta_ms, int):
            send_ack(cmd, False, "BAD_PAYLOAD", "delta_ms must be int")
            return

        # get current status to compute absolute position
        try:
            status = self.backend.get_status()
        except BackendError as e:
            self._emit_playback_error(e.code, str(e))
            if e.auth_issue:
                self._emit_auth_lost(f"seek_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            send_ack(cmd, False, e.code, "seek failed (status)")
            return

        new_pos = max(0, status.position_ms + int(delta_ms))

        try:
            self.backend.seek_abs(new_pos)
        except BackendError as e:
            self._emit_playback_error(e.code, str(e))
            if e.auth_issue:
                self._emit_auth_lost(f"seek_auth_issue:{e.code}")
            elif e.device_issue:
                self.sender.send_event("PLSM_EVENT_DISCONNECTED", {})
                self._emit_auth_lost(f"device_issue:{e.code}")
            send_ack(cmd, False, e.code, "seek failed")
            return
        send_ack(cmd, True)

    def _cmd_play(self, cmd: Dict[str, Any], payload: Dict[str, Any]) -> None:
        """
        Direct playback (explicit URI).
        """
        if not self._require_auth_ok(cmd):
            return
        uri = payload.get("uri")
        position_ms = payload.get("position_ms", 0)
        if not isinstance(uri, str) or not uri:
            send_ack(cmd, False, "BAD_PAYLOAD", "Missing uri")
            return
        if not isinstance(position_ms, int):
            send_ack(cmd, False, "BAD_PAYLOAD", "position_ms must be int")
            return

        # Stop current playback if any
        if self.state == PL_STATE_PLAYING:
            self._stop_playback()

        self._start_playback(None, uri, position_ms)
        send_ack(cmd, True)

    def _cmd_shutdown(self, cmd: Dict[str, Any]) -> None:
        if self.state == PL_STATE_PLAYING:
            self._persist_progress()
        send_ack(cmd, True)
        self.running = False

    # --------------- main loop tick --------------

    def tick(self) -> None:
        """
        Periodic tasks:
        - progress persistence
        """
        if self.state == PL_STATE_PLAYING:
            now = epoch_ms()
            if now - self.last_progress_save_ms >= PROGRESS_SAVE_INTERVAL_MS:
                self._persist_progress()
                self.last_progress_save_ms = now

    # --------------- command socket --------------

    def run(self) -> None:
        # Setup
        self.setup()
        if not self._setup_complete:
            return

        # Signal handling
        def _handle_signal(signum, frame) -> None:
            logging.info("PLSM: received signal %s, stopping", signum)
            self.running = False

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        # Command socket
        try:
            os.makedirs(os.path.dirname(CMD_SOCKET_PATH), exist_ok=True)
        except Exception:
            pass
        try:
            os.unlink(CMD_SOCKET_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            logging.error("PLSM: cannot unlink old cmd socket: %s", e)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(CMD_SOCKET_PATH)
        sock.setblocking(False)
        logging.info("PLSM: listening on %s", CMD_SOCKET_PATH)

        try:
            while self.running:
                try:
                    data = sock.recv(8192)
                except BlockingIOError:
                    data = None
                except OSError as e:
                    logging.error("PLSM: error reading cmd socket: %s", e)
                    data = None

                if data:
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except Exception as e:
                        logging.warning("PLSM: invalid JSON on cmd socket: %s", e)
                    else:
                        self.handle_command(msg)

                self.tick()
                time.sleep(MAIN_LOOP_TICK_MS / 1000.0)
        finally:
            sock.close()
            try:
                os.unlink(CMD_SOCKET_PATH)
            except FileNotFoundError:
                pass
            self.teardown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] plsm: %(message)s",
    )
    daemon = PLSMDaemon()
    daemon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
