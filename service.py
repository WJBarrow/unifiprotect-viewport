#!/usr/bin/env python3
"""UniFi Protect Alarm Manager webhook -> Viewport liveview switcher.

On a camera-motion webhook, switch a UniFi Protect Viewport to that camera's
Live View, then revert to whatever Live View was showing before, after a timeout.

Device client talks to the UniFi OS console's Protect API
(/proxy/protect/api/...) using session-cookie + X-CSRF-Token auth — the same
login flow as unifi-protect-privacy, just a different API path prefix.
"""
from __future__ import annotations

import base64
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("service")


class Config:
    def __init__(self) -> None:
        self.unifi_host = os.environ.get("UNIFI_HOST", "").rstrip("/")
        self.unifi_user = os.environ.get("UNIFI_USER", "")
        self.unifi_password = os.environ.get("UNIFI_PASSWORD", "")
        self.viewport_name = os.environ.get("VIEWPORT_NAME", "")
        self.viewer_id = os.environ.get("VIEWER_ID", "")  # optional override
        self.verify_ssl = os.environ.get("VERIFY_SSL", "false").lower() == "true"
        self.webhook_port = int(os.environ.get("WEBHOOK_PORT", "8686"))
        self.alarm_timeout = int(os.environ.get("ALARM_TIMEOUT", "30"))
        self.log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        self.log_file = os.environ.get("LOG_FILE", "")

    def validate(self) -> None:
        missing = [k for k, v in [
            ("UNIFI_HOST", self.unifi_host),
            ("UNIFI_USER", self.unifi_user),
            ("UNIFI_PASSWORD", self.unifi_password),
        ] if not v]
        if not self.viewport_name and not self.viewer_id:
            missing.append("VIEWPORT_NAME (or VIEWER_ID)")
        if missing:
            sys.exit("ERROR: missing env vars: " + ", ".join(missing))
        if self.alarm_timeout < 1:
            sys.exit("ERROR: ALARM_TIMEOUT must be >= 1")


class ProtectError(Exception):
    pass


class ViewportClient:
    """UniFi OS Protect-API client: read viewers/liveviews, set a viewer's view.

    Auth mirrors unifi-protect-privacy/bridge/app/unifi.py (session cookie +
    CSRF, auto re-login on 401), pointed at /proxy/protect/api/ instead of
    /proxy/network/api/.
    """

    def __init__(self, config: Config):
        self.config = config
        self.s = requests.Session()
        self.s.verify = config.verify_ssl
        self.csrf: str | None = None
        # VIEWPORT_NAME / VIEWER_ID may be comma-separated lists of viewports.
        self.viewport_names = [n.strip() for n in (config.viewport_name or "").split(",") if n.strip()]
        self.explicit_ids = [i.strip() for i in (config.viewer_id or "").split(",") if i.strip()]
        self.viewer_ids: list[str] = []        # resolved viewer ids to drive
        self.viewer_label: dict[str, str] = {}  # viewer id -> name (for logging)
        self.views: dict[str, str] = {}        # liveview name -> id
        self.view_names: dict[str, str] = {}    # liveview id -> name (for logging)
        self._lock = threading.Lock()           # serialize PATCH writes

    # --- auth (ported from privacy project) -------------------------------
    def login(self) -> None:
        try:
            r = self.s.post(
                f"{self.config.unifi_host}/api/auth/login",
                json={"username": self.config.unifi_user,
                      "password": self.config.unifi_password},
                timeout=15,
            )
        except requests.RequestException as e:
            raise ProtectError(f"cannot reach {self.config.unifi_host}: {e}") from e
        self.csrf = r.headers.get("X-CSRF-Token") or self._csrf_from_cookie()
        if r.status_code != 200:
            raise ProtectError(f"login failed: HTTP {r.status_code} {r.text[:200]}")

    def _csrf_from_cookie(self) -> str | None:
        tok = self.s.cookies.get("TOKEN")
        if not tok:
            return None
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("csrfToken")
        except Exception:
            return None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.csrf:
            h["X-CSRF-Token"] = self.csrf
        return h

    def _request(self, method, path, **kw):
        url = f"{self.config.unifi_host}{path}"
        try:
            r = self.s.request(method, url, headers=self._headers(), timeout=15, **kw)
            if r.status_code in (401, 403):
                self.login()
                r = self.s.request(method, url, headers=self._headers(), timeout=15, **kw)
        except requests.RequestException as e:
            raise ProtectError(f"cannot reach {self.config.unifi_host}: {e}") from e
        if r.status_code >= 400:
            raise ProtectError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else {}

    # --- domain -----------------------------------------------------------
    def _bootstrap(self) -> dict:
        return self._request("GET", "/proxy/protect/api/bootstrap")

    def connect(self) -> None:
        """Login + resolve viewer ids and the name->id Live View map."""
        self.login()
        boot = self._bootstrap()
        viewers = boot.get("viewers", []) or []
        liveviews = boot.get("liveviews", []) or []
        by_name = {v.get("name"): v.get("id") for v in viewers}
        id_to_name = {v.get("id"): v.get("name") for v in viewers}

        # Resolve the configured viewports (explicit ids + names) to viewer ids.
        resolved: list[str] = []
        for vid in self.explicit_ids:
            if vid not in id_to_name:
                raise ProtectError(f"viewer id '{vid}' not found in bootstrap")
            resolved.append(vid)
        for name in self.viewport_names:
            vid = by_name.get(name)
            if not vid:
                raise ProtectError(
                    f"viewport '{name}' not found; available viewers: {sorted(by_name)}")
            resolved.append(vid)
        seen: set[str] = set()
        self.viewer_ids = [v for v in resolved if not (v in seen or seen.add(v))]
        if not self.viewer_ids:
            raise ProtectError("no viewports configured (set VIEWPORT_NAME or VIEWER_ID)")
        self.viewer_label = {vid: id_to_name.get(vid, vid) for vid in self.viewer_ids}

        self.views = {lv.get("name"): lv.get("id") for lv in liveviews if lv.get("name")}
        self.view_names = {lv.get("id"): lv.get("name") for lv in liveviews if lv.get("id")}
        log.info("connected: viewports=%s liveviews=%s",
                 [self.viewer_label[v] for v in self.viewer_ids], sorted(self.views))

    def has_view(self, name: str) -> bool:
        return name in self.views

    def _current_liveviews(self) -> dict[str, str | None]:
        """Map each driven viewer id -> the liveview id it currently shows."""
        boot = self._bootstrap()
        cur = {v.get("id"): v.get("liveview") for v in boot.get("viewers", []) or []}
        return {vid: cur.get(vid) for vid in self.viewer_ids}

    def _set_liveview(self, viewer_id: str, liveview_id: str | None) -> None:
        with self._lock:
            self._request(
                "PATCH",
                f"/proxy/protect/api/viewers/{viewer_id}",
                json={"liveview": liveview_id},
            )

    # --- controller-facing API (snapshot / apply / restore) ---------------
    def snapshot_state(self) -> dict:
        return {"liveviews": self._current_liveviews()}

    def apply(self, view_name: str) -> None:
        lv_id = self.views.get(view_name)
        if not lv_id:
            raise ProtectError(f"unknown Live View '{view_name}'")
        for vid in self.viewer_ids:
            log.info("switching %s to '%s' (%s)", self.viewer_label[vid], view_name, lv_id)
            self._set_liveview(vid, lv_id)

    def restore(self, saved: dict) -> None:
        for vid, lv_id in (saved.get("liveviews") or {}).items():
            log.info("restoring %s to %s ('%s')",
                     self.viewer_label.get(vid, vid), lv_id, self.view_names.get(lv_id, "?"))
            self._set_liveview(vid, lv_id)


class Controller:
    IDLE, ALARMED, RESTORING = "idle", "alarmed", "restoring"

    def __init__(self, config: Config):
        self.config = config
        self.client = ViewportClient(config)
        self._lock = threading.RLock()
        self._state = self.IDLE
        self._timer: threading.Timer | None = None
        self._saved: dict = {}
        self.last_triggered = self.last_view = self.last_restored = None

    def connect(self) -> None:
        self.client.connect()

    def get_state_dict(self) -> dict:
        with self._lock:
            return {"alarm_state": self._state,
                    "last_triggered": self.last_triggered,
                    "last_view": self.last_view,
                    "last_restored": self.last_restored,
                    "views": sorted(self.client.views)}

    def trigger(self, view_name: str) -> None:
        if not self.client.has_view(view_name):
            log.warning("ignoring trigger for unknown Live View '%s'", view_name)
            return
        with self._lock:
            if self._state == self.ALARMED:
                # Re-trigger: switch to the newer view if different, but keep the
                # ORIGINAL saved view so restore returns to the real pre-motion view.
                self.last_view = view_name
                self._reset_timer()
                switch = view_name
            else:
                self._state = self.ALARMED
                self.last_triggered = _now()
                self.last_view = view_name
                switch = None  # snapshot+apply below
        try:
            if switch is None:
                self._saved = self.client.snapshot_state()
                self.client.apply(view_name)
            else:
                self.client.apply(switch)
        except Exception as exc:
            log.exception("apply failed: %s", exc)
            with self._lock:
                if switch is None:
                    self._state = self.IDLE
            return
        with self._lock:
            self._reset_timer()

    def _reset_timer(self) -> None:  # call under self._lock
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self.config.alarm_timeout, self._restore)
        self._timer.daemon = True
        self._timer.start()

    def _restore(self) -> None:
        with self._lock:
            if self._state != self.ALARMED:
                return
            self._state = self.RESTORING
        try:
            self.client.restore(self._saved)
        except Exception as exc:
            log.exception("restore failed: %s", exc)
        with self._lock:
            self._state = self.IDLE
            self.last_restored = _now()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(BaseHTTPRequestHandler):
    controller: Controller = None
    config: Config = None

    def log_message(self, fmt, *args):
        log.debug("HTTP %s — " + fmt, self.address_string(), *args)

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _view_from_query(self) -> str:
        return parse_qs(urlparse(self.path).query).get("view", [""])[0]

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/status"):
            sd = self.controller.get_state_dict()
            html = (f"<h1>Viewport switcher — port {self.config.webhook_port}</h1>"
                    f"<pre>{json.dumps(sd, indent=2)}</pre>")
            data = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/health", "/webhook"):
            self._json(200, {"status": "ok", **self.controller.get_state_dict()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/webhook":
            self._webhook()
        elif path == "/test":
            self._test()
        else:
            self._json(404, {"error": "not found"})

    def _webhook(self):
        view = self._view_from_query()
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        log.debug("webhook raw (%d bytes): %s", len(raw), raw[:2000])
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                pass
        # Protect's Alarm Manager "Test" button wraps the configured body in a
        # {"alarm_id":"TEST","text":"<json-string>"} envelope — unwrap it so the
        # test path behaves like a real detection.
        if (isinstance(data, dict) and "alarm" not in data and "Alarm" not in data
                and isinstance(data.get("text"), str)):
            try:
                inner = json.loads(data["text"])
                if isinstance(inner, dict):
                    data = inner
            except Exception:
                pass
        alarm = (data.get("alarm") or data.get("Alarm") or {}) if isinstance(data, dict) else {}
        triggers = (alarm.get("triggers") or alarm.get("Triggers") or []) if isinstance(alarm, dict) else []
        if not triggers:
            self._json(200, {"triggered": False, "reason": "no triggers"})
            return
        if not view:
            self._json(400, {"triggered": False, "reason": "missing ?view="})
            return
        keys = [t.get("key") or t.get("Key", "") for t in triggers if isinstance(t, dict)]
        log.info("webhook keys=%s view=%s", keys, view)
        threading.Thread(target=self.controller.trigger, args=(view,), daemon=True).start()
        self._json(200, {"triggered": True, "view": view, "trigger_keys": keys})

    def _test(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            data = {}
        view = data.get("view", "")
        if not view:
            self._json(400, {"triggered": False, "reason": "missing 'view'"})
            return
        threading.Thread(target=self.controller.trigger, args=(view,), daemon=True).start()
        self._json(200, {"triggered": True, "view": view})


def main():
    config = Config()
    config.validate()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    if config.log_file:
        os.makedirs(os.path.dirname(config.log_file), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            config.log_file, maxBytes=10 * 1024 * 1024, backupCount=3)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(fh)

    controller = Controller(config)
    try:
        controller.connect()
    except Exception as exc:
        sys.exit(f"ERROR: cannot connect to Protect: {exc}")

    Handler.controller = controller
    Handler.config = config
    server = HTTPServer(("0.0.0.0", config.webhook_port), Handler)

    def _shutdown(sig, _frame):
        log.info("signal %d — shutting down", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("Listening on 0.0.0.0:%d  (status → /)", config.webhook_port)
    server.serve_forever()


if __name__ == "__main__":
    main()
