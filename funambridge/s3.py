"""
S3-compatible front-end with per-account routing.

Each configured O2 account has its own access_key/secret_key. The S3 client
picks the account by which access_key it signs with: we parse the access key out
of the SigV4 `Authorization` header (we do NOT verify the signature -- bind to
localhost). That access key selects the account whose captured session is used
to talk to O2 Cloud.
"""

import datetime as _dt
import hashlib
import re
import threading
import urllib.parse
import uuid
import xml.sax.saxutils as _xml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .sapi import FunambolClient, SapiError, SessionExpired
from .store import Store
from .admin import ADMIN_PREFIX, CONSOLE_SNIPPET, render_page
from . import webdav
from . import auth
_CRED_RE = re.compile(r"Credential=([^/,\s]+)")


class Registry:
    """Builds and caches a Store per account (keyed by access_key)."""

    def __init__(self, config):
        self.config = config
        self._lock = threading.RLock()
        self._stores = {}   # access_key -> Store

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
                st = Store(client)
                self._stores[access_key] = st
            return st, acc

    def drop(self, access_key):
        with self._lock:
            self._stores.pop(access_key, None)


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _esc(s):
    return _xml.escape(str(s))


def _http_date(_v):
    return _dt.datetime.now(_dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _iso(v):
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z")
    return _now_iso()


def decode_aws_chunked(raw):
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        j = raw.find(b"\r\n", i)
        if j < 0:
            break
        size_hex = raw[i:j].split(b";", 1)[0].strip()
        try:
            size = int(size_hex, 16)
        except ValueError:
            return bytes(raw)
        i = j + 2
        if size == 0:
            break
        out += raw[i:i + size]
        i += size + 2
    return bytes(out)


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
        super().__init__(addr, S3Handler)
        self.registry = registry
        self.log = log


class S3Handler(BaseHTTPRequestHandler):
    server_version = "funambridge/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        self.server.log("s3: %s - %s" % (self.address_string(), fmt % args))

    # -- routing ------------------------------------------------------------
    def _host_candidates(self):
        cands = []
        xfh = self.headers.get("X-Forwarded-Host")
        if xfh:
            cands.append(xfh.split(",")[0].strip())
        if self.headers.get("Host"):
            cands.append(self.headers.get("Host"))
        return cands

    def _account_store(self):
        cfg = self.server.registry.config
        q = urllib.parse.urlparse(self.path).query
        access_key = auth.extract_access_key(self.headers, q)
        if not access_key:
            self._error(403, "AccessDenied", "missing S3 credentials")
            return None, None
        acc = cfg.by_access_key(access_key)
        if acc is None:
            self._error(403, "InvalidAccessKeyId", "unknown access key")
            return None, None
        if not acc.s3_enabled:
            self._error(403, "AccessDenied",
                        f"S3 is disabled for account '{acc.name}'")
            return None, None
        # require a valid SigV4 signature (proves the client knows secret_key)
        if cfg.verify_s3_signature:
            ok = auth.is_sigv4(self.headers) and auth.verify(
                self.command, self.path, self.headers, acc.secret_key,
                self._host_candidates())
            if not ok:
                self._error(403, "SignatureDoesNotMatch",
                            "S3 signature check failed (wrong secret_key, or a "
                            "proxy altered the Host header)")
                return None, None
        try:
            store, _ = self.server.registry.store_for(access_key)
        except SessionExpired as e:
            self._error(403, "AccessDenied", str(e))
            return None, None
        if store is None:
            self._error(403, "AccessDenied", "account has no captured session")
            return None, None
        return store, acc

    def _parse(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        segs = [s for s in path.split("/") if s != ""]
        bucket = segs[0] if segs else None
        key = "/".join(segs[1:]) if len(segs) > 1 else ""
        return bucket, key, qs

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if "aws-chunked" in (self.headers.get("Content-Encoding", "") or "") or \
           str(self.headers.get("x-amz-content-sha256", "")).startswith("STREAMING-"):
            raw = decode_aws_chunked(raw)
        return raw

    # -- responses ----------------------------------------------------------
    def _send_xml(self, status, root_xml, extra=None):
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n' + root_xml).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-amz-request-id", uuid.uuid4().hex)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_bytes(self, status, body, content_type, extra=None):
        extra = dict(extra or {})
        clen = extra.pop("Content-Length", str(len(body)))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", clen)
        self.send_header("Accept-Ranges", "bytes")
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, code, message):
        xml = (f"<Error><Code>{_esc(code)}</Code>"
               f"<Message>{_esc(message)}</Message>"
               f"<RequestId>{uuid.uuid4().hex}</RequestId></Error>")
        self._send_xml(status, xml)

    def _guard(self, fn):
        try:
            store, acc = self._account_store()
            if store is None:
                return
            fn(store)
        except KeyError as e:
            self._error(404, "NoSuchBucket", f"not found: {e}")
        except SessionExpired as e:
            self._error(403, "AccessDenied", f"{e}")
        except SapiError as e:
            self.server.log("SAPI error: %s" % e)
            self._error(502, "InternalError", f"upstream O2 error: {e}")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.server.log("handler error: %s" % e)
            self._error(500, "InternalError", str(e))

    # -- admin vs S3 dispatch ----------------------------------------------
    def _has_s3_auth(self):
        if self.headers.get("Authorization", "").startswith("AWS"):
            return True
        return any(k.lower().startswith("x-amz-") for k in self.headers.keys())

    def _wants_html(self):
        return "text/html" in (self.headers.get("Accept", "") or "")

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

    def _admin_get(self, path):
        cfg = self.server.registry.config
        if path == ADMIN_PREFIX + "/snippet.js":
            return self._send_raw(200, CONSOLE_SNIPPET,
                                  "text/javascript; charset=utf-8")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # honour a fronting HTTPS reverse proxy when showing the endpoint
        host = (self.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
                or self.headers.get("Host")
                or f"{cfg.s3_host}:{cfg.s3_port}")
        scheme = (self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
                  or "http")
        endpoint = f"{scheme}://{host}"
        return self._send_raw(200, render_page(
            cfg, qs.get("msg", [""])[0], qs.get("err", [""])[0], endpoint=endpoint),
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
        if path == ADMIN_PREFIX + "/set-session":
            nm = (form.get("name", [""])[0] or "").strip() or "o2"
            jid = form.get("jsessionid", [""])[0]
            vk = form.get("validationkey", [""])[0]
            plc = form.get("plc", [""])[0]
            try:
                acc = cfg.upsert_session(nm, jid, vk, plc)
            except ValueError as e:
                return self._redirect(ADMIN_PREFIX + "/?err=" + urllib.parse.quote(str(e)))
            self.server.registry.drop(acc.access_key)
            return self._redirect(ADMIN_PREFIX + "/?msg=" + urllib.parse.quote(
                f"Sesión guardada para '{acc.name}'"))
        if path == ADMIN_PREFIX + "/add":
            blob = (form.get("blob", [""])[0] or "").strip()
            name = (form.get("name", [""])[0] or "").strip() or None
            try:
                acc = cfg.upsert_from_blob(blob, name=name)
                return self._redirect(ADMIN_PREFIX + "/?msg=" +
                                      urllib.parse.quote(f"Cuenta '{acc.name}' guardada"))
            except ValueError as e:
                return self._redirect(ADMIN_PREFIX + "/?err=" +
                                      urllib.parse.quote(str(e)))
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
        if path == ADMIN_PREFIX + "/toggle":
            acc = cfg.by_name(form.get("name", [""])[0])
            if acc is None:
                return self._redirect(ADMIN_PREFIX + "/?err=cuenta+no+encontrada")
            val = form.get("value", ["1"])[0] == "1"
            what = form.get("what", [""])[0]
            if what == "s3":
                acc.s3_enabled = val
            elif what == "webdav":
                acc.webdav_enabled = val
            cfg.save()
            return self._redirect(ADMIN_PREFIX + "/?msg=" +
                                  urllib.parse.quote(f"{what} {'on' if val else 'off'} en '{acc.name}'"))
        return self._error(404, "NoSuchKey", "admin route not found")

    # -- WebDAV detection ---------------------------------------------------
    def _is_dav(self, path):
        # S3 wins if an AWS signature is present; otherwise a /dav path or HTTP
        # Basic auth (and not the admin UI) means a WebDAV client.
        if self._has_s3_auth():
            return False
        if path.startswith(webdav.DAV_PREFIX):
            return True
        return webdav.is_basic(self) and not path.startswith(ADMIN_PREFIX)

    # -- verbs --------------------------------------------------------------
    def _admin_on(self):
        return self.server.registry.config.admin_enabled

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if self._is_dav(path):
            return webdav.handle(self, "GET")
        if path.startswith(ADMIN_PREFIX):
            if not self._admin_on():
                return self._error(404, "NoSuchKey", "admin panel disabled")
            return self._admin_get(path)
        if path == "/" and not self._has_s3_auth() and self._wants_html():
            if self._admin_on():
                return self._redirect(ADMIN_PREFIX + "/")
            return self._send_raw(404, "admin panel disabled", "text/plain")
        self._guard(self._get)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if self._is_dav(path):
            return webdav.handle(self, "HEAD")
        if path.startswith(ADMIN_PREFIX):
            if not self._admin_on():
                return self._error(404, "NoSuchKey", "admin panel disabled")
            return self._admin_get(path)
        self._guard(self._head)

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if self._is_dav(path):
            return webdav.handle(self, "PUT")
        self._guard(self._put)

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if self._is_dav(path):
            return webdav.handle(self, "DELETE")
        self._guard(self._delete)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith(ADMIN_PREFIX):
            if not self._admin_on():
                return self._error(404, "NoSuchKey", "admin panel disabled")
            return self._admin_post(path)
        self._error(501, "NotImplemented", "POST is not supported")

    # -- WebDAV methods -----------------------------------------------------
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

    # -- handlers -----------------------------------------------------------
    def _get(self, store):
        bucket, key, qs = self._parse()
        if bucket is None:
            return self._list_buckets(store)
        if key == "":
            return self._list_objects(store, bucket, qs)
        return self._get_object(store, bucket, key)

    def _head(self, store):
        bucket, key, _ = self._parse()
        if bucket is None:
            return self._send_bytes(200, b"", "application/xml")
        if key == "":
            if bucket in store.list_buckets():
                return self._send_bytes(200, b"", "application/xml")
            return self._error(404, "NoSuchBucket", bucket)
        meta = store.head_object(bucket, key)
        if meta is None:
            return self._error(404, "NoSuchKey", key)
        return self._send_bytes(200, b"", meta["content_type"], {
            "ETag": f'"{meta["etag"]}"',
            "Last-Modified": _http_date(meta.get("modified")),
            "Content-Length": str(meta["size"]),
        })

    def _put(self, store):
        bucket, key, _ = self._parse()
        if bucket is None:
            return self._error(400, "InvalidRequest", "missing bucket")
        if key == "":
            store.create_bucket(bucket)
            return self._send_bytes(200, b"", "application/xml")
        data = self._read_body()
        ctype = self.headers.get("Content-Type", "application/octet-stream")
        etag = store.put_object(bucket, key, data, ctype)
        return self._send_bytes(200, b"", "application/xml",
                                {"ETag": f'"{etag}"'})

    def _delete(self, store):
        bucket, key, _ = self._parse()
        if bucket is None:
            return self._error(400, "InvalidRequest", "missing bucket")
        if key == "":
            store.delete_bucket(bucket)
        else:
            store.delete_object(bucket, key)
        return self._send_bytes(204, b"", "application/xml")

    # -- bodies -------------------------------------------------------------
    def _list_buckets(self, store):
        buckets = "".join(
            f"<Bucket><Name>{_esc(b)}</Name>"
            f"<CreationDate>{_now_iso()}</CreationDate></Bucket>"
            for b in store.list_buckets())
        xml = (f"<ListAllMyBucketsResult><Owner><ID>o2cloud</ID>"
               f"<DisplayName>o2cloud</DisplayName></Owner>"
               f"<Buckets>{buckets}</Buckets></ListAllMyBucketsResult>")
        self._send_xml(200, xml)

    def _list_objects(self, store, bucket, qs):
        prefix = qs.get("prefix", [""])[0]
        delimiter = qs.get("delimiter", [""])[0]
        objs, common = store.list_objects(bucket, prefix, delimiter)
        contents = "".join(
            f"<Contents><Key>{_esc(o['key'])}</Key>"
            f"<LastModified>{_iso(o.get('modified'))}</LastModified>"
            f"<ETag>&quot;{_esc(o['etag'])}&quot;</ETag>"
            f"<Size>{o['size']}</Size>"
            f"<StorageClass>STANDARD</StorageClass></Contents>" for o in objs)
        cps = "".join(f"<CommonPrefixes><Prefix>{_esc(p)}</Prefix>"
                      f"</CommonPrefixes>" for p in common)
        is_v2 = qs.get("list-type", ["1"])[0] == "2"
        kc = f"<KeyCount>{len(objs) + len(common)}</KeyCount>" if is_v2 else ""
        xml = (f"<ListBucketResult><Name>{_esc(bucket)}</Name>"
               f"<Prefix>{_esc(prefix)}</Prefix>"
               f"<Delimiter>{_esc(delimiter)}</Delimiter>"
               f"<MaxKeys>1000</MaxKeys>{kc}<IsTruncated>false</IsTruncated>"
               f"{contents}{cps}</ListBucketResult>")
        self._send_xml(200, xml)

    def _get_object(self, store, bucket, key):
        result = store.get_object(bucket, key)
        if result is None:
            return self._error(404, "NoSuchKey", key)
        data, ctype = result
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            try:
                a, _, b = rng[6:].partition("-")
                start = int(a) if a else 0
                end = int(b) if b else len(data) - 1
                end = min(end, len(data) - 1)
                chunk = data[start:end + 1]
                return self._send_bytes(206, chunk, ctype,
                    {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
            except ValueError:
                pass
        self._send_bytes(200, data, ctype,
                         {"ETag": f'"{hashlib.md5(data).hexdigest()}"'})
