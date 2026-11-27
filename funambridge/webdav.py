"""
WebDAV front-end (the only client protocol of funambridge, shared port with
the admin UI).

For WebDAV clients over plain HTTP (Synology Hyper Backup, WinSCP, Cyberduck,
rclone, Windows/macOS/Linux network drives...). Paths are relative to the O2
root, so the root can hold files too (not only folders). /dav/... is accepted
as an alias of /....

Auth: HTTP Basic, username = the account's user (or its name) AND password =
its password. Both must be correct.

What Hyper Backup relies on:
  * PROPFIND (Depth 0/1) with size, dates, etag and the RFC 4331 quota
    properties (free / used space of the O2 account);
  * PUT streamed to O2, GET/HEAD with byte ranges (relayed from O2, never
    loaded whole in memory), DELETE, MKCOL;
  * 503 + Retry-After when O2 fails transiently, so it retries instead of
    marking the backup as broken.
"""

import base64
import datetime as _dt
import mimetypes
import re
import urllib.parse as _up
import uuid
import xml.sax.saxutils as _x

from .sapi import SapiError, SessionExpired, first

DAV_PREFIX = "/dav"
RETRY_AFTER = "30"          # seconds a client should wait after a 503


def is_basic(handler):
    return handler.headers.get("Authorization", "").startswith("Basic ")


def _esc(s):
    return _x.escape(str(s))


def _httpdate(v):
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            v = int(v)
            ts = v / 1000 if v > 1e11 else v
            return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT")
    except Exception:  # noqa: BLE001
        pass
    return _dt.datetime.now(_dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _parts_and_prefix(handler):
    raw = _up.urlparse(handler.path).path
    prefix = ""
    if raw == DAV_PREFIX or raw.startswith(DAV_PREFIX + "/"):
        prefix = DAV_PREFIX
        raw = raw[len(DAV_PREFIX):] or "/"
    segs = [_up.unquote(s) for s in raw.split("/") if s != ""]
    return segs, prefix


def _href(prefix, parts, is_dir):
    h = prefix + "/" + "/".join(_up.quote(p) for p in parts)
    if is_dir and not h.endswith("/"):
        h += "/"
    return h or "/"


def _send(handler, code, text="", ctype="text/plain; charset=utf-8", headers=None):
    body = text.encode("utf-8") if isinstance(text, str) else text
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def _unavailable(handler, why):
    """O2 failed in a way that a later retry may fix: tell the client to retry
    (Hyper Backup treats 502/500 as fatal for the whole task)."""
    handler.server.log("webdav: O2 no disponible (%s): %s" % (handler.command, why))
    return _send(handler, 503, "O2 Cloud temporalmente no disponible: %s" % why,
                 headers={"Retry-After": RETRY_AFTER})


def _need_auth(handler):
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="funambridge"')
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _account(handler):
    """Validate Basic auth: username = user|name AND password = password."""
    cfg = handler.server.registry.config
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return None
    try:
        user, _, pw = base64.b64decode(
            auth[6:].strip()).decode("utf-8", "replace").partition(":")
    except Exception:  # noqa: BLE001
        return None
    acc = cfg.by_access_key(user) or cfg.by_name(user)
    if acc is None or pw != acc.secret_key:
        return None
    return acc


def _drain(handler, length):
    """Discard `length` bytes of request body so keep-alive stays in sync."""
    remaining = length
    while remaining > 0:
        b = handler.rfile.read(min(1 << 20, remaining))
        if not b:
            break
        remaining -= len(b)


def _store(handler, acc):
    """(store, None) or (None, error-already-sent)."""
    try:
        store, _ = handler.server.registry.store_for(acc.access_key)
    except SessionExpired as e:
        _send(handler, 403, str(e))
        return None
    if store is None:
        _send(handler, 403, "account has no captured session")
        return None
    return store


def handle(handler, method):
    length = int(handler.headers.get("Content-Length", 0) or 0)

    # PUT streams its (possibly huge) body straight through, so it must NOT be
    # pre-read into memory like the other (small, XML) bodies.
    if method == "PUT":
        return _handle_put(handler, length)

    # Drain the request body (PROPFIND/PROPPATCH/LOCK carry XML) or HTTP/1.1
    # keep-alive desyncs.
    body = handler.rfile.read(length) if length > 0 else b""

    if method == "OPTIONS":
        return _options(handler)
    acc = _account(handler)
    if acc is None:
        return _need_auth(handler)
    if method == "LOCK":
        return _lock(handler)
    if method == "UNLOCK":
        return _send(handler, 204, "")
    if method in ("MOVE", "COPY", "PROPPATCH"):
        return _send(handler, 501, "%s not supported" % method)

    store = _store(handler, acc)
    if store is None:
        return None

    segs, prefix = _parts_and_prefix(handler)
    try:
        if method == "PROPFIND":
            return _propfind(handler, store, segs, prefix, body)
        if method == "HEAD":
            return _head(handler, store, segs)
        if method == "GET":
            return _get(handler, store, segs)
        if method == "DELETE":
            return _delete(handler, store, segs)
        if method == "MKCOL":
            return _mkcol(handler, store, segs)
    except KeyError:
        return _send(handler, 404, "Not found")
    except SessionExpired as e:
        return _send(handler, 403, str(e))
    except SapiError as e:
        return _unavailable(handler, e)
    except (BrokenPipeError, ConnectionResetError):
        handler.close_connection = True       # the client went away mid-answer
        return None
    return _send(handler, 405, "Method not allowed")


def _handle_put(handler, length):
    """Streaming PUT: auth first, then stream the body to the store (which spools
    to disk if large) without ever holding the whole file in memory."""
    if "chunked" in (handler.headers.get("Transfer-Encoding", "") or "").lower():
        # We stream by Content-Length; a chunked body has no length, so refuse
        # loudly instead of silently storing an empty file. Close the connection
        # since the (chunked) body can't be length-drained.
        handler.close_connection = True
        return _send(handler, 411, "Length Required: chunked PUT not supported")
    acc = _account(handler)
    if acc is None:
        _drain(handler, length)
        return _need_auth(handler)
    store = _store(handler, acc)
    if store is None:
        _drain(handler, length)
        return None
    segs, _prefix = _parts_and_prefix(handler)
    if not segs:
        _drain(handler, length)
        return _send(handler, 409, "Cannot PUT at the root")
    limit = store.c.limits()["max_upload"]
    if length > limit:
        # refuse before receiving gigabytes O2 would reject at the end
        handler.close_connection = True
        return _send(handler, 413, "O2 Cloud no admite ficheros de más de %d MB"
                     % (limit // (1024 * 1024)))
    ctype = handler.headers.get("Content-Type", "application/octet-stream")
    try:
        store.put_stream(segs, length, ctype, handler.rfile)
    except (ValueError, OSError) as e:        # body read truncated -> unsafe conn
        handler.close_connection = True
        handler.server.log("webdav PUT read error: %s" % e)
        return _send(handler, 400, "upload incomplete")
    except SessionExpired as e:
        return _send(handler, 403, str(e))
    except SapiError as e:
        return _unavailable(handler, e)
    return _send(handler, 201, "Created")


def _options(handler):
    handler.send_response(200)
    handler.send_header("DAV", "1, 2")
    handler.send_header("Allow", "OPTIONS, GET, HEAD, PUT, DELETE, PROPFIND, "
                                 "MKCOL, LOCK, UNLOCK")
    handler.send_header("MS-Author-Via", "DAV")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _lock(handler):
    # Fake lock so clients (e.g. WinSCP) that LOCK before PUT proceed.
    token = "opaquelocktoken:" + uuid.uuid4().hex
    xml = ('<?xml version="1.0" encoding="utf-8"?>'
           '<D:prop xmlns:D="DAV:"><D:lockdiscovery><D:activelock>'
           '<D:locktype><D:write/></D:locktype>'
           '<D:lockscope><D:exclusive/></D:lockscope>'
           '<D:depth>infinity</D:depth><D:timeout>Second-3600</D:timeout>'
           f'<D:locktoken><D:href>{token}</D:href></D:locktoken>'
           '</D:activelock></D:lockdiscovery></D:prop>')
    _send(handler, 200, xml, ctype='application/xml; charset="utf-8"',
          headers={"Lock-Token": f"<{token}>"})


def _prop(href, is_collection, size=0, modified=None, name="", etag="",
          quota=None):
    if is_collection:
        body = "<D:resourcetype><D:collection/></D:resourcetype>"
        if quota is not None:                         # RFC 4331
            used, free = quota
            body += (f"<D:quota-available-bytes>{free}</D:quota-available-bytes>"
                     f"<D:quota-used-bytes>{used}</D:quota-used-bytes>")
    else:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = ("<D:resourcetype/>"
                f"<D:getcontentlength>{size}</D:getcontentlength>"
                f"<D:getcontenttype>{_esc(ctype)}</D:getcontenttype>")
        if etag:
            body += f"<D:getetag>&quot;{_esc(etag)}&quot;</D:getetag>"
    return (f"<D:response><D:href>{_esc(href)}</D:href><D:propstat><D:prop>"
            f"{body}<D:displayname>{_esc(name)}</D:displayname>"
            f"<D:getlastmodified>{_httpdate(modified)}</D:getlastmodified>"
            f"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _multistatus(handler, responses):
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>")
    _send(handler, 207, xml, ctype='application/xml; charset="utf-8"')


def _quota(handler, store):
    try:
        return store.quota()
    except SapiError as e:            # never fail a listing because of the quota
        handler.server.log("webdav: no se pudo leer la cuota: %s" % e)
        return None


def _propfind(handler, store, segs, prefix, body=b""):
    # Depth: 0 = only the resource, 1 (default) = + its children. "infinity" is
    # answered as 1 (RFC 4918 allows it; clients then walk level by level).
    deep = (handler.headers.get("Depth", "1") or "1").strip() != "0"
    responses = []
    if store.is_dir(segs):
        name = segs[-1] if segs else "o2cloud"
        responses.append(_prop(_href(prefix, segs, True), True, name=name,
                               quota=_quota(handler, store)))
        if deep:
            folders, files = store.children(segs)
            for b in folders:
                responses.append(_prop(_href(prefix, segs + [b], True),
                                       True, name=b))
            for o in files:
                responses.append(_prop(_href(prefix, segs + [o["key"]], False),
                                       False, size=o["size"], name=o["key"],
                                       modified=o.get("modified"),
                                       etag=o.get("etag", "")))
        return _multistatus(handler, responses)

    st = store.stat_path(segs)
    if st is None:
        return _send(handler, 404, "Not found")
    responses.append(_prop(_href(prefix, segs, False), False, size=st["size"],
                           name=segs[-1], modified=st["modified"],
                           etag=st["etag"]))
    return _multistatus(handler, responses)


_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$")


def parse_range(header, size):
    """(start, end) inclusive for a single-range header, None for 'whole file',
    or 'bad' if it cannot be satisfied. Suffix ranges ("bytes=-N") are turned
    into absolute offsets (O2 itself gets them wrong)."""
    if not header:
        return None
    m = _RANGE_RE.match(header)
    if not m:                      # multi-range or junk: serve the whole file
        return None
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":                    # last N bytes
        n = int(b)
        if n <= 0:
            return "bad"
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    end = min(end, size - 1)
    if start >= size or start > end:
        return "bad"
    return start, end


def send_download(handler, dl, rng=None):
    """Serve a store.Download -- bytes in memory, a file on disk, or a stream
    from O2 -- with single-range support. Nothing is loaded whole in memory:
    disk files and O2 streams are relayed in chunks, and for a range only those
    bytes are requested from O2."""
    size = dl.size
    r = parse_range(rng, size)
    if r == "bad":
        return _send(handler, 416, "Range Not Satisfiable",
                     headers={"Content-Range": f"bytes */{size}"})
    start, end = r if r else (0, size - 1)
    length = max(0, end - start + 1)
    src = None
    if dl.opener is not None and handler.command != "HEAD" and length:
        # open upstream BEFORE answering, so an O2 error can still become 503
        src = dl.opener(start if r else None, end if r else None)
    handler.send_response(206 if r else 200)
    handler.send_header("Content-Type", dl.ctype)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if r:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    if dl.etag:
        handler.send_header("ETag", f'"{dl.etag}"')
    handler.end_headers()
    if handler.command == "HEAD" or not length:
        return
    if dl.data is not None:
        handler.wfile.write(dl.data[start:end + 1])
        return
    if src is not None:
        return _relay(handler, src, start if (r and src.status == 200) else 0,
                      length)
    with open(dl.path, "rb") as fh:
        if start:
            fh.seek(start)
        left = length
        while left > 0:
            buf = fh.read(min(1 << 16, left))
            if not buf:
                break
            handler.wfile.write(buf)
            left -= len(buf)


def _relay(handler, src, skip, length):
    """Copy `length` bytes from an O2 stream to the client (after skipping
    `skip` bytes, for the rare server that ignores the Range header)."""
    try:
        while skip > 0:
            b = src.read(min(1 << 16, skip))
            if not b:
                break
            skip -= len(b)
        left = length
        while left > 0:
            b = src.read(min(1 << 16, left))
            if not b:
                break
            handler.wfile.write(b)
            left -= len(b)
        if left > 0:
            # O2 ended early: the length is already promised, so the only
            # honest thing left is to drop the connection (client retries).
            handler.server.log("webdav GET: O2 cortó la descarga (faltan %d B)" % left)
            handler.close_connection = True
    finally:
        src.close()


def _get(handler, store, segs):
    dl = store.download_path(segs)
    if dl is None:
        return _send(handler, 404, "Not found")
    send_download(handler, dl, handler.headers.get("Range"))


def _head(handler, store, segs):
    if store.is_dir(segs):
        return _send(handler, 200, "")
    st = store.stat_path(segs)
    if st is None:
        return _send(handler, 404, "Not found")
    handler.send_response(200)
    handler.send_header("Content-Type", st["ctype"])
    handler.send_header("Content-Length", str(st["size"]))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Last-Modified", _httpdate(st["modified"]))
    if st["etag"]:
        handler.send_header("ETag", f'"{st["etag"]}"')
    handler.end_headers()


def _delete(handler, store, segs):
    if not segs:
        return _send(handler, 403, "Refusing to delete the root")
    store.delete_path(segs)
    _send(handler, 204, "")


def _mkcol(handler, store, segs):
    if not segs:
        return _send(handler, 403, "No path")
    store.make_dir(segs)
    _send(handler, 201, "Created")
