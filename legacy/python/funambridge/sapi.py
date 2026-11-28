"""
Funambol OneMediaHub SAPI client (OAuth-token edition).

Authentication mirrors the official O2 Cloud Android client (reverse-engineered
from the APK -- see docs/apk-auth-ref/):

  * Every request carries  Authorization: oauth <base64(blob)>  where blob is
      {"data":{"accesstoken","refreshtoken","platform","expiresin",
               "lastrefreshdate","msisdn"}}
    (standard Base64).
  * The server may return a refreshed  Authorization: oauth <base64>  header;
    we decode it and persist the rotated tokens (auto-refresh, no separate
    refresh endpoint).
  * A SAPI session is bootstrapped with POST /sapi/login?action=login (oauth
    header, empty body); responses carry data.validationkey (CSRF), which we
    send as a query param on subsequent calls and keep refreshed.
"""

import base64
import http.client
import http.cookiejar
import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

log = logging.getLogger("funambridge.sapi")


def _redact(url):
    # never write the CSRF validationkey or download tokens to the log
    url = re.sub(r"(validationkey=)[^&]+", r"\1<redacted>", url, flags=re.I)
    url = re.sub(r"([?&]k=)[^&]+", r"\1<redacted>", url)
    return url


SAPI = {
    "login":       "/sapi/login?action=login",
    "root_folder": "/sapi/media/folder/root?action=get",
    "folder_get":  "/sapi/media/folder?action=get",
    "folder_save": "/sapi/media/folder?action=save",
    "folder_del":  "/sapi/media/folder?action=softdelete",
    "media_get":   "/sapi/media?action=get",
    "media_del":   "/sapi/media?action=softdelete",
    "storage":     "/sapi/media?action=get-storage-space&softdeleted=true",
    "system":      "/sapi/system/information?action=get",
    "profile":     "/sapi/profile?action=get",
    "upload":      "/sapi/upload",
}


class SapiError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class SessionExpired(SapiError):
    # When O2 rejects a stale validation key (SEC-1003) it returns the fresh key
    # in the error body; we carry it so the call can be retried transparently.
    def __init__(self, *a, new_validationkey=None, **kw):
        super().__init__(*a, **kw)
        self.new_validationkey = new_validationkey


def _fresh_validationkey(payload):
    """O2's SEC-1003 ('Invalid mandatory validation key') response carries the
    correct, rotated validation key in error.data. Return it (else None)."""
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if not isinstance(err, dict) or not err.get("data"):
        return None
    code = str(err.get("code") or "")
    msg = str(err.get("message") or "").lower()
    if code == "SEC-1003" or "validation key" in msg:
        return str(err["data"])
    return None


def first(d, *keys):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
    return None


def as_list(d, *keys):
    v = first(d, *keys)
    return [] if v is None else (v if isinstance(v, list) else [v])


class _MultipartBody:
    """Read-only stream = head bytes + `size` bytes of `body` + tail bytes, so a
    multipart upload streams the file without loading it whole into memory.
    `body` is bytes or a seekable file-like. seek(0) rewinds everything (used
    when a request is retried after a validation-key renewal)."""

    def __init__(self, head, body, size, tail):
        import io
        self._head, self._tail, self._size = head, tail, int(size)
        self._body = io.BytesIO(body) if isinstance(body, (bytes, bytearray)) else body
        self.length = len(head) + self._size + len(tail)
        self.seek(0)

    def seek(self, pos, whence=0):
        if pos != 0 or whence != 0:
            raise ValueError("only seek(0) is supported")
        self._part, self._off, self._left = 0, 0, self._size
        self._body.seek(0)
        return 0

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.length
        out = bytearray()
        while len(out) < n and self._part < 3:
            want = n - len(out)
            if self._part == 0:                       # multipart head
                chunk = self._head[self._off:self._off + want]
                self._off += len(chunk)
                if self._off >= len(self._head):
                    self._part, self._off = 1, 0
            elif self._part == 1:                     # the file itself
                chunk = self._body.read(min(want, self._left)) if self._left else b""
                self._left -= len(chunk)
                if not chunk or self._left <= 0:
                    if self._left > 0:
                        raise IOError("upload body shorter than declared size")
                    self._part = 2
            else:                                     # closing boundary
                chunk = self._tail[self._off:self._off + want]
                self._off += len(chunk)
                if self._off >= len(self._tail):
                    self._part = 3
            out += chunk
        return bytes(out)


# Connection errors that mean "the kept-alive socket was already dead": the
# request can be repeated once on a fresh connection.
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
          http.client.BadStatusLine, ConnectionResetError, BrokenPipeError,
          ConnectionAbortedError)

def _urllib_tls_context():
    """The exact TLS setup urllib uses (ALPN http/1.1 + post-handshake auth).
    O2 sits behind CloudFront, which fingerprints the TLS handshake: a plain
    ssl.create_default_context() (no ALPN) got an empty 401 from inside the
    Docker image (Python 3.12 / Debian OpenSSL) while urllib's worked."""
    mk = getattr(http.client, "_create_https_context", None)
    if mk is not None:
        try:
            return mk(11)
        except Exception:  # noqa: BLE001
            pass
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    if getattr(ctx, "post_handshake_auth", None) is not None:
        ctx.post_handshake_auth = True
    return ctx


_SSL_CTX = _urllib_tls_context()

# Set once O2 is seen rejecting our kept-alive connections (empty 401/403 that
# a plain urllib request does not get): from then on every request goes the
# 0.1.x way, one urllib connection per request.
_LEGACY = threading.Event()


class _Pool:
    """Persistent HTTP(S) connections, one per (thread, host).

    urllib opens (and throws away) a TCP+TLS connection per request. A backup
    makes thousands of small SAPI calls, so sockets pile up in TIME_WAIT until
    the container runs out of local ports ("[Errno 99] Cannot assign requested
    address") and every call pays a TLS handshake. Keeping one connection per
    handler thread and host fixes both. Thread-local, so no locking is needed:
    a thread never interleaves two requests on its connection."""

    def __init__(self):
        self._tl = threading.local()

    def get(self, scheme, netloc, timeout):
        conns = getattr(self._tl, "conns", None)
        if conns is None:
            conns = self._tl.conns = {}
        key = (scheme, netloc)
        c = conns.get(key)
        fresh = c is None
        if fresh:
            if scheme == "https":
                c = http.client.HTTPSConnection(netloc, timeout=timeout,
                                                context=_SSL_CTX)
            else:
                c = http.client.HTTPConnection(netloc, timeout=timeout)
            conns[key] = c
        c.timeout = timeout
        if c.sock is not None:
            c.sock.settimeout(timeout)
        return c, fresh

    def drop(self, scheme, netloc):
        c = getattr(self._tl, "conns", {}).pop((scheme, netloc), None)
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass


class _HdrView:
    """Minimal response-like wrapper: `.headers.get()` over response headers."""

    def __init__(self, msg):
        self.headers = msg


class _CookieResp:
    """Adapter so CookieJar.extract_cookies() can read an http.client response."""

    def __init__(self, resp):
        self._r = resp

    def info(self):
        return self._r.msg


class _Buffered:
    """Stream-like view of an already-read body (error answers)."""

    def __init__(self, status, headers, body):
        import io
        self.status, self.headers, self._b = status, headers, io.BytesIO(body)

    def read(self, n=-1):
        return self._b.read(n)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _UrlStream(_Buffered):
    """Stream-like wrapper of a urllib response (legacy transport)."""

    def __init__(self, resp):
        self.status, self.headers, self._r = resp.status, resp.headers, resp

    def read(self, n=-1):
        return self._r.read(n) if n and n > 0 else self._r.read()

    def close(self):
        try:
            self._r.close()
        except Exception:  # noqa: BLE001
            pass


class Stream:
    """A streaming download (body of a pooled connection). Read it to the end
    or close() it: a half-read connection cannot be reused, so close() drops it
    from the pool."""

    def __init__(self, resp, release):
        self.resp = resp
        self.status = resp.status
        self.headers = resp.msg
        self._release = release
        self._done = False

    def read(self, n=-1):
        b = self.resp.read(n) if n and n > 0 else self.resp.read()
        if not b:
            self.close()
        return b

    def close(self):
        if self._done:
            return
        self._done = True
        self._release(self.resp.isclosed())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FunambolClient:
    def __init__(self, base_url, device_id, oauth=None, session=None,
                 on_token_refresh=None,
                 user_agent="funambridge/3.0", timeout=60):
        self.base = base_url.rstrip("/")
        self.device_id = device_id or "funambridge"
        self.oauth = oauth                      # config.OAuth (token mode)
        self.session = session                  # config.Session (cookie mode)
        self.on_token_refresh = on_token_refresh
        self.user_agent = user_agent
        self.timeout = timeout

        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))
        self._pool = _Pool()
        self._info, self._info_at = None, 0.0     # /system/information cache
        self._dl_urls = {}                       # media id -> (signed url, ts)
        self.validationkey = None
        self._lock = threading.RLock()
        self._root_id = None
        self.download_base = None
        self._booted = False

        # Cookie mode: a captured web session (JSESSIONID + validationkey),
        # used when there are no OAuth tokens. Cannot self-refresh; on 401 the
        # user must re-import a fresh session.
        self.cookie_mode = bool(session and session.is_set()
                                and not (oauth and oauth.is_set()))
        if self.cookie_mode:
            host = urllib.parse.urlparse(self.base).hostname
            # O2 needs JSESSIONID + validationKey (+ PLC) cookies AND the
            # validationkey echoed as a query param (CSRF double-submit).
            self._set_cookie(host, "JSESSIONID", session.jsessionid)
            if session.validationkey:
                self._set_cookie(host, "validationKey", session.validationkey)
            if session.plc:
                self._set_cookie(host, "PLC", session.plc)
            self.validationkey = session.validationkey or None

    def _set_cookie(self, host, name, value):
        self._jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None,
            port_specified=False, domain=host, domain_specified=True,
            domain_initial_dot=False, path="/", path_specified=True,
            secure=True, expires=None, discard=False, comment=None,
            comment_url=None, rest={}, rfc2109=False))

    # -- oauth header / refresh --------------------------------------------
    def _auth_header(self):
        o = self.oauth
        data = {"platform": o.platform or "web",
                "expiresin": o.expiresin or "0",
                "lastrefreshdate": int(o.lastrefreshdate or 0)}
        if o.accesstoken:
            data["accesstoken"] = o.accesstoken
        if o.refreshtoken:
            data["refreshtoken"] = o.refreshtoken
        if o.msisdn:
            data["msisdn"] = o.msisdn
        blob = json.dumps({"data": data}, separators=(",", ":"))
        return "oauth " + base64.b64encode(blob.encode("utf-8")).decode("ascii")

    def _absorb_refreshed_token(self, resp):
        if self.cookie_mode:
            return
        hdr = resp.headers.get("Authorization")
        if not hdr or "oauth " not in hdr:
            return
        try:
            b64 = hdr.split("oauth ", 1)[1].strip()
            data = json.loads(base64.b64decode(b64).decode("utf-8")).get("data", {})
        except Exception:  # noqa: BLE001
            return
        changed = False
        with self._lock:        # guard shared token state under concurrency
            for src, dst in (("accesstoken", "accesstoken"),
                             ("refreshtoken", "refreshtoken"),
                             ("expiresin", "expiresin"), ("msisdn", "msisdn")):
                val = data.get(src)
                if val:
                    setattr(self.oauth, dst, val)
                    changed = True
            if "lastrefreshdate" in data:
                self.oauth.lastrefreshdate = int(data.get("lastrefreshdate") or 0)
                changed = True
        if changed and self.on_token_refresh:
            try:
                self.on_token_refresh(self.oauth)
            except Exception:  # noqa: BLE001
                pass

    # -- low level ----------------------------------------------------------
    def _url(self, path, extra=None, base=None):
        url = (base or self.base) + path
        if self.validationkey:
            sep = "&" if "?" in url else "?"
            url += f"{sep}validationkey={urllib.parse.quote(self.validationkey)}"
        if extra:
            sep = "&" if "?" in url else "?"
            url += sep + urllib.parse.urlencode(extra)
        return url

    def _headers(self, extra=None):
        h = {"User-Agent": self.user_agent, "Accept": "application/json",
             "X-deviceid": self.device_id, "X-request-id": uuid.uuid4().hex}
        if not self.cookie_mode and self.oauth and self.oauth.is_set():
            h["Authorization"] = self._auth_header()
        if extra:
            h.update(extra)
        return h

    def _raw(self, method, path, *, data=None, headers=None, extra_query=None,
             base=None, timeout=None, pooled=True):
        url = self._url(path, extra_query, base)
        # data may be bytes or a seekable file-like (streamed upload); rewind so
        # a retry (token refresh) re-sends from the start instead of EOF.
        n = len(data) if isinstance(data, (bytes, bytearray)) else None
        log.debug("SAPI -> %s %s%s", method, _redact(url),
                  f" ({n}B body)" if n is not None else (" (stream)" if data else ""))
        status, hdrs, body = self._send(method, url, data, self._headers(headers),
                                        timeout or self.timeout, pooled=pooled)
        self._absorb_refreshed_token(_HdrView(hdrs))
        if status >= 400:
            log.debug("SAPI <- %s %s HTTP %s: %s", method, _redact(url), status,
                      body[:300].decode("utf-8", "replace"))
            if status in (401, 403):
                newvk = None
                try:
                    newvk = _fresh_validationkey(json.loads(body.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    pass
                raise SessionExpired(
                    f"{method} {path}: unauthorized (HTTP {status}); tokens may "
                    f"be expired -- re-capture the login", status=status,
                    new_validationkey=newvk)
            raise SapiError(f"{method} {path} -> HTTP {status}: "
                            f"{body[:160].decode('utf-8','replace')}", status=status)
        log.debug("SAPI <- %s %s HTTP %s", method, _redact(url), status)
        if self.cookie_mode:
            # the server may rotate validationKey via Set-Cookie; keep the query
            # param in sync with whatever is now in the jar.
            with self._lock:
                for c in list(self._jar):     # copy: the opener may mutate it
                    if c.name == "validationKey" and c.value:
                        self.validationkey = c.value
        return _HdrView(hdrs), body

    def _send(self, method, url, data, headers, timeout, pooled=True, stream=False):
        """One HTTP exchange (see _send_direct). If O2 answers an EMPTY 401/403
        -- not a real "session expired", which comes with a body -- the request
        is repeated over urllib, like 0.1.x did; if that one works, the whole
        process switches to urllib for good (logged once)."""
        if _LEGACY.is_set():
            return self._send_urllib(method, url, data, headers, timeout, stream)
        status, hdrs, out = self._send_direct(method, url, data, headers, timeout,
                                              pooled=pooled, stream=stream)
        if status not in (401, 403):
            return status, hdrs, out
        body = out.read() if stream else out
        if stream:
            out.close()
        if not body.strip():
            try:
                st2, h2, o2 = self._send_urllib(method, url, data, headers,
                                                timeout, stream)
            except SapiError as e:
                log.debug("SAPI .. legacy retry of %s %s failed: %s", method,
                          _redact(url), e)
            else:
                if st2 < 400 and not _LEGACY.is_set():
                    _LEGACY.set()
                    log.warning("O2 rechaza las conexiones persistentes desde este "
                                "equipo (401 vacío); se usa una conexión por "
                                "petición, como en 0.1.x")
                return st2, h2, o2
        return status, hdrs, (_Buffered(status, hdrs, body) if stream else body)

    def _send_urllib(self, method, url, data, headers, timeout, stream=False):
        """The 0.1.x transport: urllib, cookies via the opener, one connection
        per request (Connection: close)."""
        if data is not None and hasattr(data, "seek"):
            data.seek(0)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers)
        try:
            resp = self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, e.headers, (_Buffered(e.code, e.headers, body)
                                       if stream else body)
        except urllib.error.URLError as e:
            raise SapiError(f"{method} {urllib.parse.urlsplit(url).path} -> "
                            f"connection error: {e.reason}")
        except OSError as e:
            raise SapiError(f"{method} {urllib.parse.urlsplit(url).path} -> "
                            f"I/O error: {e}")
        if stream:
            return resp.status, resp.headers, _UrlStream(resp)
        try:
            return resp.status, resp.headers, resp.read()
        except OSError as e:
            raise SapiError(f"{method} {urllib.parse.urlsplit(url).path} -> "
                            f"I/O error: {e}")

    def _send_direct(self, method, url, data, headers, timeout, pooled=True,
                     stream=False):
        """One HTTP exchange with cookies. Kept-alive connection by default;
        `pooled=False` uses a fresh one (uploads: a big body must never be
        replayed blindly on a socket that turned out to be dead). Returns
        (status, headers, body) or, with stream=True, (status, headers, Stream).
        Redirects are not followed (SAPI does not redirect)."""
        u = urllib.parse.urlsplit(url)
        target = u.path + ("?" + u.query if u.query else "")
        req = urllib.request.Request(url, method=method, headers=headers)
        self._jar.add_cookie_header(req)
        hdrs = dict(req.header_items())
        replayable = data is None or isinstance(data, (bytes, bytearray)) or \
            hasattr(data, "seek")
        for attempt in (0, 1):
            if pooled:
                conn, fresh = self._pool.get(u.scheme, u.netloc, timeout)
            else:
                fresh = True
                conn = (http.client.HTTPSConnection(u.netloc, timeout=timeout,
                                                    context=_SSL_CTX)
                        if u.scheme == "https" else
                        http.client.HTTPConnection(u.netloc, timeout=timeout))
            try:
                if data is not None and hasattr(data, "seek"):
                    data.seek(0)
                conn.request(method, target, body=data, headers=hdrs)
                resp = conn.getresponse()
            except _STALE as e:
                self._close(conn, u, pooled)
                if not fresh and replayable and attempt == 0:
                    log.debug("SAPI .. %s %s: kept-alive connection was closed "
                              "(%s); retrying on a new one", method, _redact(url),
                              type(e).__name__)
                    continue
                log.debug("SAPI xx %s %s connection error: %s", method,
                          _redact(url), e)
                raise SapiError(f"{method} {u.path} -> connection error: {e}")
            except OSError as e:          # timeout, refused, DNS, no local port...
                self._close(conn, u, pooled)
                log.debug("SAPI xx %s %s I/O error: %s", method, _redact(url), e)
                raise SapiError(f"{method} {u.path} -> I/O error: {e}")
            self._jar.extract_cookies(_CookieResp(resp), req)
            if stream:
                def release(done, _c=conn):
                    if not done or not pooled:
                        self._close(_c, u, pooled)
                return resp.status, resp.msg, Stream(resp, release)
            try:
                body = resp.read()
            except OSError as e:
                self._close(conn, u, pooled)
                raise SapiError(f"{method} {u.path} -> I/O error: {e}")
            if not pooled or resp.will_close:
                self._close(conn, u, pooled)
            return resp.status, resp.msg, body
        raise SapiError(f"{method} {u.path} -> connection error")  # not reached

    def _close(self, conn, u, pooled):
        if pooled:
            self._pool.drop(u.scheme, u.netloc)
        else:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _json(self, method, path, **kw):
        resp, body = self._raw(method, path, **kw)
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            raise SapiError(f"{method} {path}: non-JSON ({body[:80]!r})")
        d = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(d, dict) and d.get("validationkey"):
            with self._lock:
                self.validationkey = d["validationkey"]
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            code = err.get("code") if isinstance(err, dict) else None
            newvk = _fresh_validationkey(payload)
            if newvk:                       # stale validation key returned as 200
                raise SessionExpired(f"{method} {path}: stale validation key",
                                     code=code, new_validationkey=newvk)
            raise SapiError(f"{method} {path} -> SAPI error: {err}", code=code)
        return d if d is not None else payload

    # -- session ------------------------------------------------------------
    def ensure_session(self):
        with self._lock:
            if self._booted:
                return
            if self.cookie_mode:
                self._booted = True
                # If no validationkey was supplied, try to pick one up (the
                # profile response usually carries data.validationkey).
                if not self.validationkey:
                    try:
                        self._json("GET", SAPI["profile"])
                    except SapiError:
                        pass
                return
            # bootstrap a session using the oauth token
            self._json("POST", SAPI["login"], data=b"")
            self._booted = True

    def _set_validationkey(self, vk):
        """Adopt a rotated validation key: update the query-param value AND the
        validationKey cookie (O2 double-submits both), absorb any rotated
        PLC/JSESSIONID from the jar, and persist the refreshed web session."""
        with self._lock:
            self.validationkey = vk
            host = urllib.parse.urlparse(self.base).hostname
            self._set_cookie(host, "validationKey", vk)
            if self.session is not None:
                self.session.validationkey = vk
                for c in list(self._jar):     # copy: the opener may mutate it
                    if c.name == "PLC" and c.value:
                        self.session.plc = c.value
                    elif c.name == "JSESSIONID" and c.value:
                        self.session.jsessionid = c.value
        if self.on_token_refresh:
            try:
                self.on_token_refresh(self.oauth)   # persists config (cookie too)
            except Exception:  # noqa: BLE001
                pass

    def _call(self, method, path, **kw):
        self.ensure_session()
        renews = 0
        while True:
            try:
                return self._json(method, path, **kw)
            except SessionExpired as e:
                # O2 rotated the validation key but the web session is still
                # alive: adopt the fresh key it handed back (SEC-1003) and retry.
                # A few rounds guard against the key rotating again under load.
                if e.new_validationkey and renews < 3:
                    renews += 1
                    log.debug("validation key rotated; renewing (%d) and retrying",
                              renews)
                    self._set_validationkey(e.new_validationkey)
                    continue
                if self.cookie_mode:
                    raise  # captured web session, no token to self-refresh
                self._booted = False
                self.validationkey = None
                self.ensure_session()
                return self._json(method, path, **kw)

    def system_information(self):
        d = self._json("GET", SAPI["system"])
        self.download_base = first(d, "downloadurl", "downloadUrl")
        with self._lock:
            self._info, self._info_at = d, time.time()
        return d

    def info(self, max_age=6 * 3600):
        """/sapi/system/information (public; what the web app reads at start),
        cached. Empty dict if it cannot be fetched -- callers use defaults."""
        with self._lock:
            if self._info is not None and time.time() - self._info_at < max_age:
                return self._info
        try:
            return self.system_information()
        except SapiError as e:
            log.debug("system information: %s", e)
            with self._lock:
                self._info_at = time.time() - max_age + 300   # retry in 5 min
                return self._info or {}

    def limits(self):
        """Account limits announced by O2 (with safe defaults)."""
        d = self.info()

        def num(key, default):
            try:
                return int(str(d.get(key, default)).strip())
            except (TypeError, ValueError):
                return default
        return {
            "max_upload": num("sapi.upload.max-size-in-mb", 4096) * 1024 * 1024,
            "folder_ops": num("folders.softdelete-updates-limit", 1000),
            "trash_days": num("trash.cleanup.items-expire-time-in-minutes",
                              86400) // 1440,
        }

    def validate_session(self):
        return self._call("GET", SAPI["profile"])

    # -- folders ------------------------------------------------------------
    def root_id(self):
        if self._root_id is not None:
            return self._root_id
        with self._lock:                     # one-time init, guarded
            if self._root_id is None:
                d = self._call("GET", SAPI["root_folder"])
                folders = as_list(d, "folders", "folder")
                fid = (first(folders[0], "id", "folderid") if folders
                       else first(d, "id", "folderid"))
                self._root_id = str(fid) if fid is not None else None
            return self._root_id

    def list_folders(self, parent_id):
        d = self._call("GET", SAPI["folder_get"],
                       extra_query={"parentid": parent_id})
        folders = as_list(d, "folders", "folder", "items")
        # The API ignores parentid and returns a flat list (incl. the root),
        # so keep only the direct children of parent_id.
        pid = str(parent_id)
        return [f for f in folders
                if str(first(f, "parentid", "parentId") or "") == pid]

    def create_folder(self, parent_id, name):
        body = json.dumps({"data": {"name": name, "parentid": parent_id}}).encode()
        d = self._call("POST", SAPI["folder_save"], data=body,
                       headers={"Content-Type": "application/json"})
        fid = first(d, "id", "folderid")
        if fid is None:
            sub = first(d, "folder")          # nested {"folder":{"id":..}}
            if isinstance(sub, dict):
                fid = first(sub, "id", "folderid")
        if fid is None:
            subs = as_list(d, "folders", "folder")   # {"folders":[{"id":..}]}
            if subs:
                fid = first(subs[0], "id", "folderid")
        if fid is None:
            # The save returned no id in any known shape: re-list the parent and
            # find the just-created folder by name (never return "None").
            for f in self.list_folders(parent_id):
                if str(first(f, "name", "foldername")) == name:
                    fid = first(f, "id", "folderid")
                    break
        if fid is None:
            raise SapiError(f"create_folder('{name}'): no folder id in response")
        return str(fid)

    def delete_folder(self, fid):
        self._call("GET", SAPI["folder_del"], extra_query={"id": fid})

    # -- media --------------------------------------------------------------
    # Media listing is a POST whose body asks for the fields we want; a plain
    # GET returns only id/date (a sync digest). "mime"/"contenttype" are NOT
    # valid field names here, so content types are guessed from the name.
    MEDIA_FIELDS = ["name", "size", "modificationdate", "creationdate", "etag"]

    def list_media(self, folder_id, limit=200):
        if folder_id is None or str(folder_id) in ("", "None"):
            raise SapiError(f"list_media: invalid folder id {folder_id!r}")
        items, offset = [], 0
        body = json.dumps({"data": {"fields": self.MEDIA_FIELDS}}).encode()
        while True:
            d = self._call(
                "POST",
                f"/sapi/media?action=get&folderid={folder_id}"
                f"&limit={limit}&offset={offset}",
                data=body, headers={"Content-Type": "application/json"})
            batch = as_list(d, "media", "items", "files")
            items.extend(batch)
            if not batch or not (isinstance(d, dict) and d.get("more")):
                break
            offset += len(batch)
        return items

    def delete_media(self, media_id):
        self._call("GET", SAPI["media_del"], extra_query={"id": media_id})

    def storage_space(self):
        return self._call("GET", SAPI["storage"])

    def _upload_base(self):
        """Upload host as announced in /system/information
        ("sapi.upload.endpoint"); falls back to upload.<web host>."""
        ep = str(self.info().get("sapi.upload.endpoint") or "").strip().rstrip("/")
        if ep.startswith("https://"):
            return ep
        p = urllib.parse.urlparse(self.base)
        return f"{p.scheme}://upload.{p.netloc}"

    def upload(self, folder_id, name, size, body, content_type="application/octet-stream"):
        # Since ~19 Nov 2026 O2 rejects the old two-step upload
        # (/sapi/upload/file?action=save-metadata + save, now 401 with an empty
        # body). The web client uploads in ONE multipart request to the upload
        # host:  POST upload.<host>/sapi/upload?action=save
        #   part "data": {"data":{"name","size","modificationdate","folderid"}}
        #   part "file": the bytes (application/octet-stream)
        # answered with {"success":..., "id":..., "status":"V", "etag":...}.
        # `body` is bytes or a seekable file-like of exactly `size` bytes; it is
        # streamed inside the multipart envelope, never loaded whole in memory.
        # (`content_type` is kept for the call signature; O2 infers the type
        # from the name, like the web client which always sends octet-stream.)
        fid = int(folder_id) if str(folder_id).isdigit() else folder_id
        meta = json.dumps({"data": {"name": name, "size": size,
                                    "modificationdate": "", "folderid": fid}},
                          ensure_ascii=False, separators=(",", ":"))
        boundary = "----funambridge" + uuid.uuid4().hex
        fname = name.replace("\r", " ").replace("\n", " ").replace('"', "%22")
        head = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="data"\r\n\r\n'
                f"{meta}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("ascii")
        stream = _MultipartBody(head, body, size, tail)
        # The socket timeout applies to every send/recv; after the last byte O2
        # may take a while (big files) before answering. Giving up too early is
        # what made clients retry an upload O2 had in fact stored -> duplicates.
        tmo = max(self.timeout, 120 + size // (1 << 20))
        d = self._call("POST", "/sapi/upload?action=save", data=stream,
                       base=self._upload_base(), timeout=tmo, pooled=False,
                       headers={"Content-Type":
                                f"multipart/form-data; boundary={boundary}",
                                "Content-Length": str(stream.length)})
        new_id = first(d, "id", "mediaid")
        if not new_id:
            raise SapiError(f"upload: no id in response ({str(d)[:160]})")
        return str(new_id)

    def download_url(self, media_id):
        """The pre-signed download url of a media item, by id."""
        d = self._call("POST", "/sapi/media?action=get",
                       data=json.dumps({"data": {"ids": [int(media_id)],
                                                 "fields": ["url"]}}).encode(),
                       headers={"Content-Type": "application/json"})
        got = as_list(d, "media", "items")
        url = first(got[0], "url") if got else None
        if not url:
            raise SapiError(f"no download url for media {media_id}")
        return self.base + url if url.startswith("/") else url

    def open_download(self, media_id, timeout=600):
        """Open a streaming download of a whole media item (caller reads/closes)."""
        return self.open_media(media_id, timeout=timeout)

    _URL_TTL = 20 * 60          # signed download urls are reused for a while

    def _signed_url(self, media_id, fresh=False):
        mid = str(media_id)
        with self._lock:
            hit = self._dl_urls.get(mid)
            if hit and not fresh and time.time() - hit[1] < self._URL_TTL:
                return hit[0]
        url = self.download_url(mid)
        with self._lock:
            if len(self._dl_urls) > 2000:
                self._dl_urls.clear()
            self._dl_urls[mid] = (url, time.time())
        return url

    def open_media(self, media_id, start=None, end=None, timeout=600):
        """Stream a media item, or only bytes start..end (inclusive) of it.
        O2 honours absolute ranges (206) but mishandles suffix ranges
        ("bytes=-N" returns the *start*), so callers pass absolute offsets.
        Returns a Stream with .status (200/206), .headers and .read()."""
        hdr = {}
        if start is not None:
            hdr["Range"] = f"bytes={int(start)}-" + ("" if end is None else str(int(end)))
        for attempt in (0, 1):
            url = self._signed_url(media_id, fresh=attempt > 0)
            status, hdrs, st = self._send("GET", url, None, self._headers(hdr),
                                          timeout, stream=True)
            if status in (200, 206):
                return st
            st.read()                    # drain the error body, keep the socket
            st.close()
            if attempt == 0 and status in (401, 403, 404, 410):
                log.debug("download %s: HTTP %s with a cached url; asking a new one",
                          media_id, status)
                continue
            if status in (401, 403):
                raise SessionExpired(f"download {media_id}: HTTP {status}",
                                     status=status)
            raise SapiError(f"download {media_id} -> HTTP {status}", status=status)
        raise SapiError(f"download {media_id}: no url")  # not reached

    def download(self, item):
        url = first(item, "url", "downloadurl", "downloadUrl")
        if not url:
            # ask for this item's pre-signed download url by id
            mid = first(item, "id", "mediaid")
            try:
                d = self._call("POST", "/sapi/media?action=get",
                               data=json.dumps({"data": {"ids": [int(mid)],
                                               "fields": ["url"]}}).encode(),
                               headers={"Content-Type": "application/json"})
                got = as_list(d, "media", "items")
                url = first(got[0], "url") if got else None
            except (ValueError, TypeError, SapiError):
                url = None
        if not url:
            raise SapiError("no download url for media item")
        if url.startswith("/"):
            url = self.base + url
        status, hdrs, body = self._send("GET", url, None, self._headers(),
                                        self.timeout)
        if status >= 400:
            raise SapiError(f"download -> HTTP {status}", status=status)
        return body, hdrs.get("Content-Type", "application/octet-stream")
