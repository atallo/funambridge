"""
Minimal WebDAV front-end, sharing the same port as the S3 API + admin UI.

For file managers that speak WebDAV over plain HTTP (WinSCP, Cyberduck, Windows
"Map network drive", Nautilus, rclone :webdav, ...). It maps onto the same Store
by *path relative to the O2 root*, so the root can hold files too (not only
folders).

Auth: HTTP Basic where username = account access_key (or its name) AND password =
secret_key. Both must be correct. The account must have webdav_enabled.

Routing (in s3.S3Handler): WebDAV-only methods (PROPFIND, MKCOL, LOCK, ...)
always come here; GET/PUT/DELETE/HEAD come here when the request uses HTTP Basic
auth (vs an AWS signature). Paths may be at the root (/folder/file) or under /dav.
"""

import base64
import datetime as _dt
import mimetypes
import urllib.parse as _up
import uuid
import xml.sax.saxutils as _x

from .sapi import SapiError, SessionExpired, first

DAV_PREFIX = "/dav"
DAV_METHODS = {"PROPFIND", "PROPPATCH", "MKCOL", "MOVE", "COPY", "LOCK", "UNLOCK"}


def is_basic(handler):
    return handler.headers.get("Authorization", "").startswith("Basic ")


def _esc(s):
    return _x.escape(str(s))


def _httpdate(v):
    try:
        if isinstance(v, (int, float)):
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


def _need_auth(handler):
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="funambridge"')
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _account(handler):
    """Validate Basic auth: username = access_key|name AND password = secret_key."""
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


def handle(handler, method):
    # Drain the request body (PROPFIND/PROPPATCH/LOCK carry XML; PUT the file)
    # or HTTP/1.1 keep-alive desyncs.
    length = int(handler.headers.get("Content-Length", 0) or 0)
    body = handler.rfile.read(length) if length > 0 else b""

    if method == "OPTIONS":
        return _options(handler)
    acc = _account(handler)
    if acc is None:
        return _need_auth(handler)
    if not acc.webdav_enabled:
        return _send(handler, 403, "WebDAV is disabled for this account")
    if method == "LOCK":
        return _lock(handler)
    if method == "UNLOCK":
        return _send(handler, 204, "")
    if method in ("MOVE", "COPY", "PROPPATCH"):
        return _send(handler, 501, "%s not supported" % method)

    try:
        store, _ = handler.server.registry.store_for(acc.access_key)
    except SessionExpired as e:
        return _send(handler, 403, str(e))
    if store is None:
        return _send(handler, 403, "account has no captured session")

    segs, prefix = _parts_and_prefix(handler)
    try:
        if method == "PROPFIND":
            return _propfind(handler, store, segs, prefix)
        if method in ("GET", "HEAD"):
            return _get(handler, store, segs)
        if method == "PUT":
            return _put(handler, store, segs, body)
        if method == "DELETE":
            return _delete(handler, store, segs)
        if method == "MKCOL":
            return _mkcol(handler, store, segs)
    except KeyError:
        return _send(handler, 404, "Not found")
    except SessionExpired as e:
        return _send(handler, 403, str(e))
    except SapiError as e:
        handler.server.log("webdav SAPI error: %s" % e)
        return _send(handler, 502, "upstream error: %s" % e)
    return _send(handler, 405, "Method not allowed")


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


def _prop(href, is_collection, size=0, modified=None, name=""):
    if is_collection:
        body = "<D:resourcetype><D:collection/></D:resourcetype>"
    else:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body = ("<D:resourcetype/>"
                f"<D:getcontentlength>{size}</D:getcontentlength>"
                f"<D:getcontenttype>{_esc(ctype)}</D:getcontenttype>")
    return (f"<D:response><D:href>{_esc(href)}</D:href><D:propstat><D:prop>"
            f"{body}<D:displayname>{_esc(name)}</D:displayname>"
            f"<D:getlastmodified>{_httpdate(modified)}</D:getlastmodified>"
            f"</D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>")


def _multistatus(handler, responses):
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n'
           '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "</D:multistatus>")
    _send(handler, 207, xml, ctype='application/xml; charset="utf-8"')


def _propfind(handler, store, segs, prefix):
    deep = handler.headers.get("Depth", "1") != "0"
    responses = []
    if store.is_dir(segs):
        name = segs[-1] if segs else "o2cloud"
        responses.append(_prop(_href(prefix, segs, True), True, name=name))
        if deep:
            folders, files = store.children(segs)
            for b in folders:
                responses.append(_prop(_href(prefix, segs + [b], True),
                                       True, name=b))
            for o in files:
                responses.append(_prop(_href(prefix, segs + [o["key"]], False),
                                       False, size=o["size"], name=o["key"],
                                       modified=o.get("modified")))
        return _multistatus(handler, responses)

    m = store.find_media(segs)
    if m is None:
        return _send(handler, 404, "Not found")
    responses.append(_prop(_href(prefix, segs, False), False,
                           size=int(first(m, "size", "filesize") or 0),
                           name=segs[-1],
                           modified=first(m, "modificationdate", "creationdate")))
    return _multistatus(handler, responses)


def _get(handler, store, segs):
    res = store.download_path(segs)
    if res is None:
        return _send(handler, 404, "Not found")
    data, ctype = res
    rng = handler.headers.get("Range")
    if rng and rng.startswith("bytes="):
        try:
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else len(data) - 1
            end = min(end, len(data) - 1)
            return _send(handler, 206, data[start:end + 1], ctype, {
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Accept-Ranges": "bytes"})
        except ValueError:
            pass
    _send(handler, 200, data, ctype, {"Accept-Ranges": "bytes"})


def _put(handler, store, segs, data):
    if not segs:
        return _send(handler, 409, "Cannot PUT at the root")
    store.put_file(segs, data,
                   handler.headers.get("Content-Type", "application/octet-stream"))
    _send(handler, 201, "Created")


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
