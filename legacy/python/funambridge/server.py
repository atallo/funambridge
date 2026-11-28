"""
HTTP server of funambridge: one port serving WebDAV (to O2 Cloud) and the
admin web UI under /_admin.

Each configured O2 account has its own WebDAV user/password (stored as
access_key/secret_key in the config); the Basic-auth user picks the account
whose O2 session is used.
"""

import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .sapi import FunambolClient, SapiError, SessionExpired
from .store import Store
from .admin import ADMIN_PREFIX, render_page, cache_rows
from . import webdav
from . import config as _cfgmod

log = logging.getLogger("funambridge.keepalive")


class Registry:
    """Builds and caches a Store per account (keyed by access_key)."""

    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._stores = {}   # access_key -> Store
        self._pending = {}  # token -> SMS login waiting for its code
        # access_key -> {"session", "ok", "fail", "err", "since"} (keepalive)
        self._alive = {}
        self._stop = threading.Event()

    def _cache_base(self):
        return (self.config.cache_dir
                or os.path.join(tempfile.gettempdir(), "funambridge-cache"))

    def store_for(self, access_key):
        with self._lock:
            acc = self.config.by_access_key(access_key)
            if acc is None:
                return None, None
            st = self._stores.get(access_key)
            if st is None:
                if not acc.has_auth():
                    raise SessionExpired(
                        f"account '{acc.name}' has no auth yet -- add tokens or "
                        f"import a HAR (see the admin page)")

                def _persist(_oauth, _acc=acc):
                    self.config.save()

                client = FunambolClient(acc.base_url, acc.device_id,
                                        oauth=acc.oauth, session=acc.session,
                                        on_token_refresh=_persist)
                disk_dir = os.path.join(self._cache_base(), access_key)
                st = Store(client, cache_ttl=acc.cache_seconds,
                           disk_dir=disk_dir)
                self._stores[access_key] = st
            return st, acc

    def drop(self, access_key):
        with self._lock:
            st = self._stores.pop(access_key, None)
        if st is not None:
            st.close()                 # stop its cache sweeper thread

    # -- keepalive --------------------------------------------------------
    # Since Nov 2026 O2 ends a web session after ~1-2 h WITHOUT requests (it
    # used to survive a whole day idle). The web app polls every minute; we
    # make one cheap authenticated call every few minutes so a session that is
    # only used by a nightly backup does not die in between.
    def start_keepalive(self):
        mins = int(getattr(self.config, "keepalive_minutes", 0) or 0)
        if mins <= 0:
            log.info("keepalive desactivado (keepalive_minutes: 0)")
            return
        threading.Thread(target=self._keepalive_loop, args=(mins * 60,),
                         daemon=True, name="funambridge-keepalive").start()
        log.info("keepalive: ping a O2 cada %d min por cuenta", mins)

    def stop_keepalive(self):
        self._stop.set()

    def _keepalive_loop(self, every):
        while not self._stop.wait(every):
            for acc in list(self.config.accounts):
                try:
                    self.ping(acc)
                except Exception as ex:  # noqa: BLE001
                    log.debug("keepalive %s: %s", acc.name, ex)

    def ping(self, acc):
        """One authenticated call to keep `acc`'s session alive. Stops pinging
        a session O2 has already rejected (until it is renewed)."""
        if not acc.has_auth():
            return None
        sid = acc.session.jsessionid or acc.oauth.accesstoken
        with self._lock:
            st_ = self._alive.get(acc.access_key)
            if st_ is None or st_["session"] != sid:
                st_ = {"session": sid, "ok": 0, "fail": 0, "err": "",
                       "since": time.time()}
                self._alive[acc.access_key] = st_
            if st_["fail"]:
                return False             # already dead: wait for a renewal
        try:
            store, _ = self.store_for(acc.access_key)
            store.c.validate_session()
        except SessionExpired as ex:
            with self._lock:
                st_["fail"], st_["err"] = time.time(), str(ex)
            log.warning("la sesión de O2 de '%s' ha caducado (viva %.1f h, "
                        "último ping bueno %s); renuévala desde el panel",
                        acc.name, (time.time() - st_["since"]) / 3600,
                        time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(st_["ok"])) if st_["ok"]
                        else "ninguno")
            return False
        except SapiError as ex:              # network hiccup: try next round
            with self._lock:
                st_["err"] = str(ex)
            log.debug("keepalive %s: %s", acc.name, ex)
            return None
        with self._lock:
            st_["ok"], st_["err"] = time.time(), ""
        log.debug("keepalive %s: sesión viva", acc.name)
        return True

    def alive_overview(self):
        """{access_key: {"ok": ts|0, "fail": ts|0, "since": ts}} for the
        sessions currently configured (stale entries are ignored)."""
        out = {}
        with self._lock:
            for acc in self.config.accounts:
                st_ = self._alive.get(acc.access_key)
                sid = acc.session.jsessionid or acc.oauth.accesstoken
                if st_ and st_["session"] == sid:
                    out[acc.access_key] = dict(st_)
        return out

    def cache_overview(self):
        """{access_key: [cache entries]} for accounts with a live Store."""
        with self._lock:
            return {ak: st.cache.snapshot() for ak, st in self._stores.items()}

    # -- SMS logins waiting for their code (in memory, short-lived) ---------
    PENDING_TTL = 600

    def _purge_pending(self):
        now = time.time()
        for tok in [t for t, p in self._pending.items()
                    if now - p["ts"] > self.PENDING_TTL]:
            self._pending.pop(tok, None)

    def add_pending(self, info):
        with self._lock:
            self._purge_pending()
            tok = uuid.uuid4().hex
            self._pending[tok] = dict(info, ts=time.time())
            return tok

    def get_pending(self, token):
        with self._lock:
            self._purge_pending()
            return self._pending.get(token or "")

    def pop_pending(self, token):
        with self._lock:
            return self._pending.pop(token or "", None)


def _parse_multipart(body, content_type):
    """Minimal multipart/form-data parser (stdlib `cgi` is gone in 3.13+).
    Returns {field_name: bytes}."""
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return {}
    delim = b"--" + m.group(1).strip().strip('"').encode()
    fields = {}
    for part in body.split(delim):
        part = part.lstrip(b"\r\n")
        if not part or part.startswith(b"--"):
            continue
        hdr_end = part.find(b"\r\n\r\n")
        if hdr_end < 0:
            continue
        headers = part[:hdr_end].decode("utf-8", "replace")
        value = part[hdr_end + 4:]
        if value.endswith(b"\r\n"):
            value = value[:-2]
        mn = re.search(r'name="([^"]*)"', headers)
        if mn:
            fields[mn.group(1)] = value
    return fields


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, registry, log=print):
        super().__init__(addr, Handler)
        self.registry = registry
        self.log = log


class Handler(BaseHTTPRequestHandler):
    server_version = "funambridge/3.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        self.server.log("http: %s - %s" % (self.address_string(), fmt % args))

    def _wants_html(self):
        return "text/html" in (self.headers.get("Accept", "") or "")

    def _not_found(self, msg="not found"):
        return self._send_raw(404, msg, "text/plain; charset=utf-8")

    def _send_raw(self, status, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _admin_browse(self, cfg):
        """GET /_admin/browse.json?name=<cuenta>&path=<a/b>: check the account's
        credentials against O2 (real profile call) and list that folder fresh
        from the server. Always answers JSON {ok, ...} so the panel can show why
        it failed (session expired, missing folder, upstream error)."""
        import json
        import time
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        name = qs.get("name", [""])[0]
        parts = [p for p in qs.get("path", [""])[0].split("/") if p]

        def out(obj):
            return self._send_raw(200, json.dumps(obj),
                                  "application/json; charset=utf-8")

        acc = cfg.by_name(name)
        if acc is None:
            return out({"ok": False, "kind": "noaccount",
                        "error": "cuenta no encontrada"})
        if not acc.has_auth():
            return out({"ok": False, "kind": "noauth",
                        "error": "la cuenta no tiene sesión guardada"})
        try:
            store, _ = self.server.registry.store_for(acc.access_key)
            store.c.validate_session()          # real call: credentials alive?
            res = store.browse(parts)
            try:
                sp = store.c.storage_space() or {}
                res["storage"] = {"used": sp.get("used"), "quota": sp.get("quota"),
                                  "trash": sp.get("softdeleted")}
            except SapiError:
                pass
            res["limits"] = store.c.limits()
            res.update(ok=True, path="/".join(parts),
                       checked=time.strftime("%H:%M:%S"))
            return out(res)
        except SessionExpired as e:
            return out({"ok": False, "kind": "session", "error": str(e)})
        except KeyError:
            return out({"ok": False, "kind": "notfound",
                        "error": "carpeta no encontrada: /" + "/".join(parts)})
        except SapiError as e:
            return out({"ok": False, "kind": "upstream", "error": str(e)})
        except Exception as e:  # noqa: BLE001  (never answer HTML to the panel)
            self.server.log("admin browse error: %s" % e)
            return out({"ok": False, "kind": "error", "error": str(e)})

    def _admin_get(self, path):
        cfg = self.server.registry.config
        if path == ADMIN_PREFIX + "/cache.json":
            import json
            rows = cache_rows(cfg, self.server.registry.cache_overview())
            return self._send_raw(200, json.dumps(rows),
                                  "application/json; charset=utf-8")
        if path == ADMIN_PREFIX + "/browse.json":
            return self._admin_browse(cfg)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # honour a fronting HTTPS reverse proxy when showing the endpoint
        host = (self.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
                or self.headers.get("Host")
                or f"{cfg.host}:{cfg.port}")
        scheme = (self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
                  or "http")
        endpoint = f"{scheme}://{host}"
        err = qs.get("err", [""])[0]
        pending = None
        tok = qs.get("login", [""])[0]
        if tok:
            p = self.server.registry.get_pending(tok)
            if p is None:
                err = err or "Ese inicio de sesión ha caducado: vuelve a pedir el SMS"
            else:
                pending = {"token": tok, "name": p["name"], "masked": p["masked"]}
        return self._send_raw(200, render_page(
            cfg, qs.get("msg", [""])[0], err, endpoint=endpoint,
            cache_overview=self.server.registry.cache_overview(),
            pending=pending, alive=self.server.registry.alive_overview()),
            "text/html; charset=utf-8")

    def _admin_post(self, path):
        cfg = self.server.registry.config
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        if path == ADMIN_PREFIX + "/import-har":
            from . import har
            parts = _parse_multipart(body, self.headers.get("Content-Type", ""))
            har_bytes = parts.get("har", b"")
            name = parts.get("name", b"").decode("utf-8", "replace").strip() or None
            try:
                data = har.parse_har_data(har_bytes)
                acc = cfg.upsert_from_har(data, name=name)
            except Exception as e:  # noqa: BLE001
                return self._redirect(ADMIN_PREFIX + "/?err=" +
                                      urllib.parse.quote("HAR: " + str(e)))
            self.server.registry.drop(acc.access_key)  # rebuild client with new session
            return self._redirect(ADMIN_PREFIX + "/?msg=" + urllib.parse.quote(
                f"Sesión importada para '{acc.name}'"
                + ("" if acc.session.validationkey else " (sin validationkey)")))

        form = urllib.parse.parse_qs(body.decode("utf-8"))
        q = urllib.parse.quote
        if path == ADMIN_PREFIX + "/login-start":
            # step 1 of the SMS login: start O2's OAuth flow and send the SMS
            from .login import O2Login, LoginError
            nm = (form.get("name", [""])[0] or "").strip() or "o2"
            acc = cfg.by_name(nm)
            # keep a stable device per account (each login registers a device)
            dev = acc.device_id if acc and acc.device_id.startswith("web-") else None
            lg = O2Login(acc.base_url if acc else _cfgmod.DEFAULT_BASE_URL, dev)
            try:
                lg.start(form.get("phone", [""])[0])
            except LoginError as e:
                return self._redirect(ADMIN_PREFIX + "/?err=" + q("SMS: " + str(e)))
            tok = self.server.registry.add_pending(
                {"login": lg, "name": nm, "masked": "···" + lg.phone[-3:]})
            return self._redirect(ADMIN_PREFIX + "/?login=" + tok + "&msg=" + q(
                f"SMS enviado a ···{lg.phone[-3:]}: escribe el código"))
        if path == ADMIN_PREFIX + "/login-verify":
            # step 2: check the SMS code and store the new session
            from .login import LoginError
            tok = form.get("token", [""])[0]
            p = self.server.registry.get_pending(tok)
            if p is None:
                return self._redirect(ADMIN_PREFIX + "/?err=" + q(
                    "Ese inicio de sesión ha caducado: vuelve a pedir el SMS"))
            try:
                res = p["login"].verify(form.get("code", [""])[0])
            except LoginError as e:
                return self._redirect(ADMIN_PREFIX + "/?login=" + tok + "&err=" + q(str(e)))
            self.server.registry.pop_pending(tok)
            acc = cfg.upsert_session(p["name"], res["jsessionid"], res["validationkey"],
                                     "", base_url=res["base_url"],
                                     device_id=res["device_id"])
            acc.oauth.msisdn = res["msisdn"]
            cfg.save()
            self.server.registry.drop(acc.access_key)   # rebuild with new session
            return self._redirect(ADMIN_PREFIX + "/?msg=" + q(
                f"Sesión iniciada para '{acc.name}'"))
        if path == ADMIN_PREFIX + "/login-cancel":
            self.server.registry.pop_pending(form.get("token", [""])[0])
            return self._redirect(ADMIN_PREFIX + "/")
        if path == ADMIN_PREFIX + "/set-session":
            nm = (form.get("name", [""])[0] or "").strip() or "o2"
            jid = form.get("jsessionid", [""])[0]
            vk = form.get("validationkey", [""])[0]
            plc = form.get("plc", [""])[0]
            dev = form.get("deviceid", [""])[0]
            try:
                acc = cfg.upsert_session(nm, jid, vk, plc, device_id=dev)
            except ValueError as e:
                return self._redirect(ADMIN_PREFIX + "/?err=" + urllib.parse.quote(str(e)))
            self.server.registry.drop(acc.access_key)
            return self._redirect(ADMIN_PREFIX + "/?msg=" + urllib.parse.quote(
                f"Sesión guardada para '{acc.name}'"))
        if path == ADMIN_PREFIX + "/remove":
            cfg.remove_account(form.get("name", [""])[0])
            cfg.save()
            return self._redirect(ADMIN_PREFIX + "/?msg=" +
                                  urllib.parse.quote("Cuenta eliminada"))
        if path == ADMIN_PREFIX + "/keys":
            acc = cfg.by_name(form.get("name", [""])[0])
            if acc is None:
                return self._redirect(ADMIN_PREFIX + "/?err=cuenta+no+encontrada")
            old = acc.access_key
            if form.get("mode", [""])[0] == "set":
                acc.set_keys(form.get("access_key", [""])[0],
                             form.get("secret_key", [""])[0])
            else:
                acc.set_keys(None, None)  # random
            cfg.save()
            self.server.registry.drop(old)
            self.server.registry.drop(acc.access_key)
            return self._redirect(ADMIN_PREFIX + "/?msg=" +
                                  urllib.parse.quote(f"Claves actualizadas para '{acc.name}'"))
        if path == ADMIN_PREFIX + "/cache":
            acc = cfg.by_name(form.get("name", [""])[0])
            if acc is None:
                return self._redirect(ADMIN_PREFIX + "/?err=cuenta+no+encontrada")
            try:
                acc.cache_seconds = max(0, int(form.get("seconds", ["0"])[0] or 0))
            except ValueError:
                return self._redirect(ADMIN_PREFIX + "/?err=valor+invalido")
            cfg.save()
            self.server.registry.drop(acc.access_key)   # recrea el Store con el TTL nuevo
            return self._redirect(ADMIN_PREFIX + "/?msg=" +
                                  urllib.parse.quote(f"caché {acc.cache_seconds}s en '{acc.name}'"))
        return self._not_found("admin route not found")

    # -- routing: /_admin = panel, everything else = WebDAV -------------------
    def _admin_on(self):
        return self.server.registry.config.admin_enabled

    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _is_admin(self, path):
        return path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")

    def do_GET(self):
        path = self._path()
        if self._is_admin(path):
            if not self._admin_on():
                return self._not_found("admin panel disabled")
            return self._admin_get(path)
        if path == "/" and not webdav.is_basic(self) and self._wants_html():
            # a browser opening the root: show the panel
            if self._admin_on():
                return self._redirect(ADMIN_PREFIX + "/")
        return webdav.handle(self, "GET")

    def do_HEAD(self):
        path = self._path()
        if self._is_admin(path):
            if not self._admin_on():
                return self._not_found("admin panel disabled")
            return self._admin_get(path)
        return webdav.handle(self, "HEAD")

    def do_POST(self):
        path = self._path()
        if self._is_admin(path):
            if not self._admin_on():
                return self._not_found("admin panel disabled")
            return self._admin_post(path)
        return self._send_raw(405, "POST is not supported", "text/plain")

    def do_PUT(self):
        webdav.handle(self, "PUT")

    def do_DELETE(self):
        webdav.handle(self, "DELETE")

    def do_OPTIONS(self):
        webdav.handle(self, "OPTIONS")

    def do_PROPFIND(self):
        webdav.handle(self, "PROPFIND")

    def do_MKCOL(self):
        webdav.handle(self, "MKCOL")

    def do_MOVE(self):
        webdav.handle(self, "MOVE")

    def do_COPY(self):
        webdav.handle(self, "COPY")

    def do_PROPPATCH(self):
        webdav.handle(self, "PROPPATCH")

    def do_LOCK(self):
        webdav.handle(self, "LOCK")

    def do_UNLOCK(self):
        webdav.handle(self, "UNLOCK")
