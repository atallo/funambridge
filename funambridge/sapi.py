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
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

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
    pass


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
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(headers))
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            self._absorb_refreshed_token(e)
            body = e.read()
            if e.code in (401, 403):
                raise SessionExpired(
                    f"{method} {path}: unauthorized (HTTP {e.code}); tokens may "
                    f"be expired -- re-capture the login", status=e.code)
            raise SapiError(f"{method} {path} -> HTTP {e.code}: "
                            f"{body[:160].decode('utf-8','replace')}", status=e.code)
        except urllib.error.URLError as e:
            raise SapiError(f"{method} {path} -> connection error: {e.reason}")
        self._absorb_refreshed_token(resp)
        if self.cookie_mode:
            # the server may rotate validationKey via Set-Cookie; keep the query
            # param in sync with whatever is now in the jar.
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
            self.validationkey = d["validationkey"]
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            code = err.get("code") if isinstance(err, dict) else None
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

    def _call(self, method, path, **kw):
        self.ensure_session()
        try:
            return self._json(method, path, **kw)
        except SessionExpired:
            if self.cookie_mode:
                raise  # cannot self-refresh a captured web session
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
        return str(first(self._call("POST", SAPI["folder_save"], data=body,
                   headers={"Content-Type": "application/json"}), "id", "folderid"))

    def delete_folder(self, fid):
        self._call("GET", SAPI["folder_del"], extra_query={"id": fid})

    # -- media --------------------------------------------------------------
    # Media listing is a POST whose body asks for the fields we want; a plain
    # GET returns only id/date (a sync digest). "mime"/"contenttype" are NOT
    # valid field names here, so content types are guessed from the name.
    MEDIA_FIELDS = ["name", "size", "modificationdate", "creationdate", "etag"]

    def list_media(self, folder_id, limit=200):
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

    def upload(self, folder_id, name, data, content_type="application/octet-stream"):
        # Two-step upload (as the official client does):
        #  1) save-metadata -> returns the new item id;
        #  2) POST the bytes with X-funambol-id = that id.
        ctype = content_type or "application/octet-stream"
        meta = {"data": {"name": name, "size": len(data),
                         "contenttype": ctype, "folderid": folder_id}}
        d = self._call("POST", "/sapi/upload/file?action=save-metadata",
                       data=json.dumps(meta).encode(),
                       headers={"Content-Type": "application/json"})
        new_id = first(d, "id", "mediaid")
        if not new_id:
            raise SapiError("upload: save-metadata returned no id")
        self._call("POST", "/sapi/upload/file?action=save", data=data,
                   headers={"Content-Type": ctype,
                            "X-funambol-file-size": str(len(data)),
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
