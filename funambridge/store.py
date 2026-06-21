"""
Maps S3 (bucket, key) / WebDAV paths onto OneMediaHub folders/media for a single
account.

  bucket          = a folder directly under the account root
  key "a/b/c.txt" = sub-folders a, b + file c.txt inside `bucket`

Write cache: after O2 accepts an upload it takes a few/several seconds to make
the item appear in listings/downloads (async processing). A short per-account
cache (cache_seconds, 0 = disabled) remembers just-uploaded files so listings,
HEAD and GET include them until the server catches up. This stops backup tools
that read a file right after writing it from failing.

Cached content lives in memory up to a budget; anything that does not fit spills
to a file on disk (one file per entry under disk_dir) and is streamed straight
from there on download, so large files never have to sit in RAM to be served.
"""

import hashlib
import logging
import mimetypes
import os
import tempfile
import threading
import time

from .sapi import FunambolClient, SapiError, first

log = logging.getLogger("funambridge.store")

# memory budget for cached file *content* (metadata is always kept while fresh);
# content above the remaining budget is spilled to disk instead.
_CONTENT_BUDGET = 256 * 1024 * 1024

# An incoming upload is read into RAM up to this size; bigger ones spill to a
# temp file on disk as they stream in, so a large PUT never sits in memory.
_SPOOL_MAX = 16 * 1024 * 1024


def read_upload(reader, length, spill_dir, mem_max=None):
    """Read exactly `length` bytes from `reader`, keeping them in RAM if small or
    spilling to a temp file on disk if large, computing the md5 in one pass.
    Returns (data_or_None, path_or_None, total_read, md5_hex)."""
    if mem_max is None:
        mem_max = _SPOOL_MAX
    h = hashlib.md5()
    buf = bytearray()
    path = fh = None
    total = 0
    remaining = length
    try:
        while remaining > 0:
            chunk = reader.read(min(1 << 20, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            total += len(chunk)
            h.update(chunk)
            if path is None:
                buf.extend(chunk)
                if len(buf) > mem_max:                # roll over to disk
                    os.makedirs(spill_dir, exist_ok=True)
                    fd, path = tempfile.mkstemp(suffix=".up", dir=spill_dir)
                    fh = os.fdopen(fd, "wb")
                    fh.write(buf)
                    buf = bytearray()
            else:
                fh.write(chunk)
    finally:
        if fh:
            fh.close()
    if path is None:
        return bytes(buf), None, total, h.hexdigest()
    return None, path, total, h.hexdigest()


class _WriteCache:
    """Recently-uploaded files, keyed by full path tuple, kept for `ttl` secs.

    Content fits in RAM up to _CONTENT_BUDGET; bigger files spill to a file
    under `disk_dir` (one .bin per entry) instead of being dropped."""

    def __init__(self, ttl, disk_dir=None):
        self.ttl = int(ttl or 0)
        self.disk_dir = disk_dir or None
        self._e = {}          # tuple(parts) -> entry
        self._bytes = 0       # RAM held by cached content
        self._lock = threading.RLock()

    def enabled(self):
        return self.ttl > 0

    def _fresh(self, e):
        return (time.time() - e["ts"]) < self.ttl

    def _room(self):
        return _CONTENT_BUDGET - self._bytes

    def _disk_path(self, key):
        h = hashlib.sha256("/".join(key).encode("utf-8", "replace")).hexdigest()[:32]
        return os.path.join(self.disk_dir, h + ".bin")

    def _spill(self, key, data):
        os.makedirs(self.disk_dir, exist_ok=True)
        path = self._disk_path(key)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

    def put_path(self, parts, size, etag, ctype, src_path, media_id=None):
        """Adopt an already-written file (a streamed upload's temp file) as this
        entry's on-disk content, by moving it into the cache dir. Returns True if
        adopted (caller must not delete src_path then), False otherwise."""
        if self.ttl <= 0 or not self.disk_dir:
            return False
        with self._lock:
            key = tuple(parts)
            self._drop(key)
            try:
                os.makedirs(self.disk_dir, exist_ok=True)
                dst = self._disk_path(key)
                os.replace(src_path, dst)
            except OSError as ex:
                log.debug("cache adopt failed for %s: %s", "/".join(parts), ex)
                return False
            self._e[key] = {"ts": time.time(), "name": parts[-1], "size": size,
                            "etag": etag, "ctype": ctype, "data": None,
                            "disk": dst, "id": media_id,
                            "mtime": int(time.time() * 1000)}
            log.debug("cache put %s (%dB, ttl=%ss, disk-adopt)",
                      "/".join(parts), size, self.ttl)
            return True

    def put(self, parts, size, etag, ctype, data, media_id=None):
        if self.ttl <= 0:
            return
        with self._lock:
            key = tuple(parts)
            self._drop(key)
            mem, disk, where = None, None, "meta-only"
            if data is not None:
                if len(data) <= self._room():           # fits the RAM budget
                    mem, where = data, "ram"
                elif self.disk_dir:                      # too big -> spill to disk
                    try:
                        disk, where = self._spill(key, data), "disk"
                    except OSError as ex:                # never fail the upload
                        log.debug("cache spill failed for %s: %s",
                                  "/".join(parts), ex)
            self._e[key] = {"ts": time.time(), "name": parts[-1], "size": size,
                            "etag": etag, "ctype": ctype, "data": mem,
                            "disk": disk, "id": media_id,
                            "mtime": int(time.time() * 1000)}
            self._bytes += len(mem) if mem else 0
            log.debug("cache put %s (%dB, ttl=%ss, %s)",
                      "/".join(parts), size, self.ttl, where)

    def _drop(self, key):
        e = self._e.pop(key, None)
        if not e:
            return
        if e.get("data") is not None:
            self._bytes -= len(e["data"])
        if e.get("disk"):
            try:
                os.remove(e["disk"])
            except OSError:
                pass

    def drop(self, parts):
        with self._lock:
            self._drop(tuple(parts))

    def get(self, parts):
        if self.ttl <= 0:
            return None
        with self._lock:
            e = self._e.get(tuple(parts))
            if e is None:
                return None
            if not self._fresh(e):
                self._drop(tuple(parts))
                return None
            return e

    def children(self, parts):
        if self.ttl <= 0:
            return []
        pt = tuple(parts)
        out = []
        with self._lock:
            for k in list(self._e):
                if k[:-1] != pt:
                    continue
                e = self._e[k]
                if self._fresh(e):
                    out.append(e)
                else:
                    self._drop(k)
        return out

    def snapshot(self):
        """Current entries for the admin view: path, size, where, expiry (s)."""
        now = time.time()
        out = []
        with self._lock:
            for k, e in self._e.items():
                if not self._fresh(e):
                    continue
                where = ("disco" if e.get("disk")
                         else "memoria" if e.get("data") is not None
                         else "solo metadatos")
                out.append({"path": "/".join(k), "size": e["size"],
                            "where": where,
                            "expires_in": max(0.0, self.ttl - (now - e["ts"]))})
        out.sort(key=lambda x: x["path"])
        return out


def _synth(e):
    """Build a media-item-like dict from a cache entry (marked _cached)."""
    return {"_cached": True, "_data": e.get("data"), "_path": e.get("disk"),
            "id": e.get("id"), "name": e["name"], "size": e["size"],
            "etag": e["etag"], "contenttype": e["ctype"],
            "modificationdate": e["mtime"]}


class Download:
    """A file ready to serve: bytes in memory (`data`) or a file on disk
    (`path`) to stream. Exactly one of the two is set."""
    __slots__ = ("ctype", "size", "etag", "data", "path")

    def __init__(self, ctype, size, etag="", data=None, path=None):
        self.ctype = ctype
        self.size = int(size)
        self.etag = etag or ""
        self.data = data
        self.path = path


class Store:
    def __init__(self, client: FunambolClient, cache_ttl=0, root_bucket="",
                 disk_dir=None):
        self.c = client
        self._lock = threading.RLock()
        self._folder_cache = {}
        # Per-folder {name: id} index. Loaded once per folder, then kept in sync
        # with our own uploads/deletes, so a PUT can check for an existing
        # same-name item in O(1) instead of re-listing the whole folder, and so
        # it still finds a copy we just uploaded that the server has not made
        # visible yet (a fresh listing or the short write-cache would miss it).
        self._media_index = {}            # folder_id(str) -> {name: id}
        self._mi_lock = threading.RLock()
        # Fixed pool of locks to serialize PUTs of the same (folder, name).
        self._put_locks = [threading.Lock() for _ in range(64)]
        self.cache = _WriteCache(cache_ttl, disk_dir=disk_dir)
        # A fresh Store starts with an empty index, so any spill files left in
        # disk_dir by a previous run are orphans -- clear them.
        if disk_dir and os.path.isdir(disk_dir):
            for f in os.listdir(disk_dir):
                if f.endswith((".bin", ".tmp")):
                    try:
                        os.remove(os.path.join(disk_dir, f))
                    except OSError:
                        pass
        # Optional virtual bucket that maps to the O2 root, so the files sitting
        # directly at the root (which ListBuckets can't show) are reachable via
        # S3 inside this bucket. "" disables it.
        self.root_bucket = root_bucket or ""

    def _base_parts(self, bucket):
        """Path parts (relative to O2 root) for a bucket: [] for the virtual
        root bucket, else [bucket]."""
        if self.root_bucket and bucket == self.root_bucket:
            return []
        return [bucket]


    # -- folder resolution --------------------------------------------------
    def _children_folders(self, parent_id):
        out = {}
        for f in self.c.list_folders(parent_id):
            name = first(f, "name", "foldername")
            fid = first(f, "id", "folderid", "folderId")
            if name is not None and fid is not None:
                out[str(name)] = str(fid)
        return out

    def resolve_folder(self, parts, create=False):
        with self._lock:
            key = tuple(parts)
            if key in self._folder_cache:
                return self._folder_cache[key]
            cur = self.c.root_id()
            acc = []
            for name in parts:
                acc.append(name)
                cached = self._folder_cache.get(tuple(acc))
                if cached:
                    cur = cached
                    continue
                kids = self._children_folders(cur)
                if name in kids:
                    cur = kids[name]
                elif create:
                    cur = self.c.create_folder(cur, name)
                else:
                    return None
                self._folder_cache[tuple(acc)] = cur
            return cur

    def invalidate(self):
        with self._lock:
            self._folder_cache.clear()

    # -- per-folder media index (name -> {id,size,modified,etag}) -----------
    def _media_index_for(self, folder_id):
        """A folder's media indexed by name, loaded once then kept in sync with
        our own uploads/deletes. Lets PUT dedup and reads (list/HEAD/find) be
        served without re-listing the whole folder each time."""
        fid = str(folder_id)
        with self._mi_lock:
            idx = self._media_index.get(fid)
            if idx is not None:
                return idx
        loaded = {}
        for m in self.c.list_media(folder_id):       # one-time scan per folder
            nm = first(m, "name", "filename")
            mid = first(m, "id", "mediaid")
            if nm is not None and mid is not None:
                loaded[str(nm)] = {
                    "id": str(mid),
                    "size": int(first(m, "size", "filesize") or 0),
                    "modified": first(m, "modificationdate", "creationdate"),
                    "etag": str(first(m, "etag", "id") or ""),
                }
        with self._mi_lock:
            idx = self._media_index.get(fid)
            if idx is None:
                self._media_index[fid] = loaded
                idx = loaded
            return idx

    def _index_set(self, folder_id, name, mid, size, modified, etag):
        with self._mi_lock:
            idx = self._media_index.get(str(folder_id))
        if idx is not None:
            idx[name] = {"id": str(mid), "size": int(size),
                         "modified": modified, "etag": etag}

    def _index_pop(self, folder_id, name):
        with self._mi_lock:
            idx = self._media_index.get(str(folder_id))
        if idx is not None:
            idx.pop(name, None)

    @staticmethod
    def _item_from_index(name, ent):
        """A media-item-like dict built from an index entry (for reads)."""
        return {"id": ent["id"], "name": name, "size": ent["size"],
                "modificationdate": ent.get("modified"),
                "etag": ent.get("etag", "")}

    def _put_lock(self, folder_id, name):
        # Same (folder, name) always maps to the same lock, so two concurrent
        # PUTs of one file can't both create it (O2 would keep 'name' AND
        # 'name (1)'). The fixed pool bounds memory.
        return self._put_locks[hash((str(folder_id), name)) % len(self._put_locks)]

    # -- buckets ------------------------------------------------------------
    def list_buckets(self):
        names = sorted(self._children_folders(self.c.root_id()).keys())
        if self.root_bucket and self.root_bucket not in names:
            names = [self.root_bucket] + names
        return names

    def create_bucket(self, name):
        if name == self.root_bucket:
            return                      # virtual bucket: nothing to create
        self.resolve_folder([name], create=True)
        self.invalidate()

    def delete_bucket(self, name):
        if name == self.root_bucket:
            raise ValueError("cannot delete the virtual root bucket")
        fid = self.resolve_folder([name])
        if fid is None:
            raise KeyError(name)
        self.c.delete_folder(fid)
        self.invalidate()

    # -- objects (S3 bucket/key) -------------------------------------------
    @staticmethod
    def _split_key(key):
        parts = [p for p in key.split("/") if p != ""]
        if not parts:
            return [], None
        return parts[:-1], parts[-1]

    def list_objects(self, bucket, prefix="", delimiter=""):
        base = self._base_parts(bucket)
        if base and self.resolve_folder(base) is None:
            raise KeyError(bucket)
        pdirs = [p for p in prefix.split("/")[:-1] if p]
        base_id = self.resolve_folder(base + pdirs)
        objects, common = [], set()
        if base_id is None:
            return objects, sorted(common)
        base_prefix = "/".join(pdirs) + ("/" if pdirs else "")
        seen = set()
        for name, ent in self._media_index_for(base_id).items():
            full = base_prefix + name
            if not full.startswith(prefix):
                continue
            seen.add(name)
            e = self.cache.get(base + pdirs + [name])     # freshest size if cached
            if e is not None:
                objects.append({"key": full, "size": e["size"],
                                "etag": e["etag"], "modified": e["mtime"]})
            else:
                objects.append({"key": full, "size": ent["size"],
                                "etag": ent.get("etag", ""),
                                "modified": ent.get("modified")})
        for e in self.cache.children(base + pdirs):       # not-yet-indexed uploads
            if e["name"] in seen:
                continue
            full = base_prefix + e["name"]
            if full.startswith(prefix):
                objects.append({"key": full, "size": e["size"], "etag": e["etag"],
                                "modified": e["mtime"]})
        if delimiter == "/":
            for sub in self._children_folders(base_id):
                cp = base_prefix + sub + "/"
                if cp.startswith(prefix) or prefix.startswith(cp):
                    common.add(cp)
        return objects, sorted(common)

    def _find_item(self, bucket, key):
        dirs, fname = self._split_key(key)
        if fname is None:
            return None
        return self.find_media(self._base_parts(bucket) + dirs + [fname])

    def head_object(self, bucket, key):
        m = self._find_item(bucket, key)
        if m is None:
            return None
        name = str(first(m, "name", "filename") or key)
        ctype = (first(m, "contenttype", "mimetype")
                 or mimetypes.guess_type(name)[0] or "application/octet-stream")
        return {
            "size": int(first(m, "size", "filesize") or 0),
            "etag": str(first(m, "etag", "id") or ""),
            "content_type": ctype,
            "modified": first(m, "modificationdate", "creationdate"),
        }

    def get_object(self, bucket, key):
        m = self._find_item(bucket, key)
        if m is None:
            return None
        return self._download(m)

    def _overwrite(self, parts, folder_id, fname):
        """A PUT replaces. Delete any current item with this name before
        uploading, or O2 keeps the old one and stores the new one as
        'name (1).ext'. Uses the per-folder name->id index, so it is O(1) per
        PUT and also catches a copy we just uploaded that the server has not
        made visible yet."""
        try:
            idx = self._media_index_for(folder_id)
        except SapiError as ex:
            log.debug("overwrite: index load %s: %s", folder_id, ex)
            idx = None
        if idx is not None:
            ent = idx.get(fname)
            if ent:
                try:
                    self.c.delete_media(ent["id"])
                except SapiError as ex:
                    log.debug("overwrite: delete %s: %s", "/".join(parts), ex)
                self._index_pop(folder_id, fname)
        self.cache.drop(parts)

    def _finish_put(self, parts, total, md5, content_type, data, path):
        """Shared tail of every upload: overwrite any prior copy, push the bytes
        (from RAM `data` or the temp file `path`) to O2, update the index and the
        write cache. Exactly one of `data`/`path` is set."""
        fid = self.resolve_folder(parts[:-1], create=True)
        fname = parts[-1]
        adopted = False
        try:
            with self._put_lock(fid, fname):
                self._overwrite(parts, fid, fname)
                if data is not None:
                    new_id = self.c.upload(fid, fname, total, data, content_type)
                    self._index_set(fid, fname, new_id, total,
                                    int(time.time() * 1000), md5)
                    self.cache.put(parts, total, md5, content_type, data,
                                   media_id=new_id)
                else:
                    with open(path, "rb") as fh:
                        new_id = self.c.upload(fid, fname, total, fh, content_type)
                    self._index_set(fid, fname, new_id, total,
                                    int(time.time() * 1000), md5)
                    adopted = self.cache.put_path(parts, total, md5, content_type,
                                                  path, media_id=new_id)
            return md5
        finally:
            if path is not None and not adopted:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def put_object(self, bucket, key, data, content_type):
        dirs, fname = self._split_key(key)
        if fname is None:
            raise ValueError("empty key")
        parts = self._base_parts(bucket) + dirs + [fname]
        return self._finish_put(parts, len(data), hashlib.md5(data).hexdigest(),
                                content_type, data, None)

    def put_object_stream(self, bucket, key, size, content_type, reader):
        dirs, fname = self._split_key(key)
        if fname is None:
            raise ValueError("empty key")
        return self.put_stream(self._base_parts(bucket) + dirs + [fname],
                               size, content_type, reader)

    def put_stream(self, parts, size, content_type, reader):
        """Stream an upload of declared length `size` from `reader` (the request
        body) without buffering the whole file in RAM."""
        parts = list(parts)
        spill = self.cache.disk_dir or tempfile.gettempdir()
        data, path, total, md5 = read_upload(reader, size, spill)
        if total != size:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            raise ValueError(f"incomplete upload: got {total} of {size} bytes")
        return self._finish_put(parts, total, md5, content_type, data, path)

    def delete_object(self, bucket, key):
        dirs, fname = self._split_key(key)
        if fname is not None:
            self.delete_path(self._base_parts(bucket) + dirs + [fname])

    # -- path-based API (used by WebDAV; parts relative to the O2 root) ------
    def _obj(self, key, m):
        return {
            "key": key,
            "size": int(first(m, "size", "filesize") or 0),
            "etag": str(first(m, "etag", "id") or ""),
            "modified": first(m, "modificationdate", "lastupdate", "creationdate"),
        }

    def is_dir(self, parts):
        return self.resolve_folder(list(parts)) is not None

    def children(self, parts):
        """(subfolder_names, file_objs) of the folder at parts ([] = root)."""
        fid = self.resolve_folder(list(parts))
        if fid is None:
            raise KeyError("/".join(parts))
        names = sorted(self._children_folders(fid).keys())
        files, seen = [], set()
        for nm, ent in self._media_index_for(fid).items():
            seen.add(nm)
            e = self.cache.get(list(parts) + [nm])        # freshest size if cached
            if e is not None:
                files.append({"key": nm, "size": e["size"],
                              "etag": e["etag"], "modified": e["mtime"]})
            else:
                files.append({"key": nm, "size": ent["size"],
                              "etag": ent.get("etag", ""),
                              "modified": ent.get("modified")})
        for e in self.cache.children(parts):              # not-yet-indexed uploads
            if e["name"] not in seen and e["name"] not in names:
                files.append({"key": e["name"], "size": e["size"],
                              "etag": e["etag"], "modified": e["mtime"]})
        return names, files

    def find_media(self, parts):
        if not parts:
            return None
        e = self.cache.get(parts)
        if e is not None:
            return _synth(e)                 # just-uploaded: serve from cache
        parent = self.resolve_folder(list(parts[:-1]))
        if parent is None:
            return None
        try:
            ent = self._media_index_for(parent).get(parts[-1])
        except SapiError:
            return None
        return self._item_from_index(parts[-1], ent) if ent else None

    def _download(self, m):
        ctype = m.get("contenttype") or "application/octet-stream"
        if m.get("_cached"):
            etag = str(m.get("etag") or "")
            if m.get("_data") is not None:
                return Download(ctype, len(m["_data"]), etag=etag, data=m["_data"])
            path = m.get("_path")
            if path and os.path.exists(path):
                return Download(ctype, os.path.getsize(path), etag=etag, path=path)
            return None                  # content not retained (no disk dir / spill failed)
        data, dctype = self.c.download(m)
        return Download(dctype or ctype, len(data),
                        etag=hashlib.md5(data).hexdigest(), data=data)

    def put_file(self, parts, data, content_type):
        if not parts:
            raise ValueError("no file name")
        self._finish_put(list(parts), len(data), hashlib.md5(data).hexdigest(),
                         content_type, data, None)

    def make_dir(self, parts):
        self.resolve_folder(list(parts), create=True)
        self.invalidate()

    def delete_path(self, parts):
        if not parts:
            return
        fid = self.resolve_folder(list(parts))
        if fid is not None:
            self.c.delete_folder(fid)
            with self._mi_lock:
                self._media_index.pop(str(fid), None)   # folder gone
            self.invalidate()
            return
        m = self.find_media(parts)
        self.cache.drop(parts)
        if m is not None:
            mid = first(m, "id", "mediaid")
            if mid:                       # cached items now carry the server id too
                self.c.delete_media(mid)
            parent = self.resolve_folder(list(parts[:-1]))
            if parent is not None:
                self._index_pop(parent, parts[-1])

    def download_path(self, parts):
        m = self.find_media(parts)
        return None if m is None else self._download(m)
