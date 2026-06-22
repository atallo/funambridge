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
import http.cookiejar
import json
import logging
import re
import threading
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
    def _url(self, path, extra=None):
        url = self.base + path
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

    def _raw(self, method, path, *, data=None, headers=None, extra_query=None):
        url = self._url(path, extra_query)
        # data may be bytes or a seekable file-like (streamed upload); rewind so
        # a retry (token refresh) re-sends from the start instead of EOF.
        if data is not None and hasattr(data, "seek"):
            try:
                data.seek(0)
            except (OSError, ValueError):
                pass
        n = len(data) if isinstance(data, (bytes, bytearray)) else None
        log.debug("SAPI -> %s %s%s", method, _redact(url),
                  f" ({n}B body)" if n is not None else (" (stream)" if data else ""))
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(headers))
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            self._absorb_refreshed_token(e)
            body = e.read()
            log.debug("SAPI <- %s %s HTTP %s: %s", method, _redact(url), e.code,
                      body[:300].decode("utf-8", "replace"))
            if e.code in (401, 403):
                newvk = None
                try:
                    newvk = _fresh_validationkey(json.loads(body.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    pass
                raise SessionExpired(
                    f"{method} {path}: unauthorized (HTTP {e.code}); tokens may "
                    f"be expired -- re-capture the login", status=e.code,
                    new_validationkey=newvk)
            raise SapiError(f"{method} {path} -> HTTP {e.code}: "
                            f"{body[:160].decode('utf-8','replace')}", status=e.code)
        except urllib.error.URLError as e:
            log.debug("SAPI xx %s %s connection error: %s", method, _redact(url),
                      e.reason)
            raise SapiError(f"{method} {path} -> connection error: {e.reason}")
        log.debug("SAPI <- %s %s HTTP %s", method, _redact(url), resp.status)
        self._absorb_refreshed_token(resp)
        if self.cookie_mode:
            # the server may rotate validationKey via Set-Cookie; keep the query
            # param in sync with whatever is now in the jar.
            with self._lock:
                for c in self._jar:
                    if c.name == "validationKey" and c.value:
                        self.validationkey = c.value
        return resp, resp.read()

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
                for c in self._jar:
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
        return d

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

    def upload(self, folder_id, name, size, body, content_type="application/octet-stream"):
        # Two-step upload (as the official client does):
        #  1) save-metadata -> returns the new item id;
        #  2) POST the bytes with X-funambol-id = that id.
        # `body` is bytes or a seekable file-like of exactly `size` bytes; with a
        # file-like + explicit Content-Length, http.client streams it in blocks
        # (the whole file never has to sit in memory).
        ctype = content_type or "application/octet-stream"
        meta = {"data": {"name": name, "size": size,
                         "contenttype": ctype, "folderid": folder_id}}
        d = self._call("POST", "/sapi/upload/file?action=save-metadata",
                       data=json.dumps(meta).encode(),
                       headers={"Content-Type": "application/json"})
        new_id = first(d, "id", "mediaid")
        if not new_id:
            raise SapiError("upload: save-metadata returned no id")
        self._call("POST", "/sapi/upload/file?action=save", data=body,
                   headers={"Content-Type": ctype,
                            "Content-Length": str(size),
                            "X-funambol-file-size": str(size),
                            "X-funambol-id": str(new_id)})
        return str(new_id)

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
        req = urllib.request.Request(url, headers=self._headers())
        resp = self._opener.open(req, timeout=self.timeout)
        self._absorb_refreshed_token(resp)
        return resp.read(), resp.headers.get("Content-Type",
                                             "application/octet-stream")
