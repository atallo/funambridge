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
import threading
import time

from .sapi import FunambolClient, SapiError, first

log = logging.getLogger("funambridge.store")

# memory budget for cached file *content* (metadata is always kept while fresh);
# content above the remaining budget is spilled to disk instead.
_CONTENT_BUDGET = 256 * 1024 * 1024


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

    def _spill(self, key, data):
        os.makedirs(self.disk_dir, exist_ok=True)
        h = hashlib.sha256("/".join(key).encode("utf-8", "replace")).hexdigest()[:32]
        path = os.path.join(self.disk_dir, h + ".bin")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return path

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
        for m in self.c.list_media(base_id):
            name = first(m, "name", "filename")
            if name is None:
                continue
            self.cache.drop(base + pdirs + [str(name)])  # server has it now
            full = base_prefix + str(name)
            if not full.startswith(prefix):
                continue
            seen.add(str(name))
            objects.append(self._obj(full, m))
        for e in self.cache.children(base + pdirs):       # not-yet-visible uploads
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
        """A PUT replaces. Delete any current item with this name first, or O2
        keeps the old one and stores the new upload as 'name (1).ext'. Covers
        both a copy still only in the write cache (just uploaded, not yet
        listable -- deleted by its server id) and one already visible."""
        e = self.cache.get(parts)
        if e and e.get("id"):
            try:
                self.c.delete_media(e["id"])
            except SapiError as ex:
                log.debug("overwrite: cached delete %s: %s", "/".join(parts), ex)
        self.cache.drop(parts)
        try:
            for m in self.c.list_media(folder_id):
                if str(first(m, "name", "filename")) == fname:
                    mid = first(m, "id", "mediaid")
                    if mid:
                        self.c.delete_media(mid)
        except SapiError as ex:
            log.debug("overwrite: server scan %s: %s", fname, ex)

    def put_object(self, bucket, key, data, content_type):
        dirs, fname = self._split_key(key)
        if fname is None:
            raise ValueError("empty key")
        parts = self._base_parts(bucket) + dirs + [fname]
        fid = self.resolve_folder(parts[:-1], create=True)
        self._overwrite(parts, fid, fname)
        new_id = self.c.upload(fid, fname, data, content_type)
        etag = hashlib.md5(data).hexdigest()
        self.cache.put(parts, len(data), etag, content_type, data, media_id=new_id)
        return etag

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
        for m in self.c.list_media(fid):
            nm = first(m, "name", "filename")
            if nm is None:
                continue
            self.cache.drop(list(parts) + [str(nm)])      # server has it now
            seen.add(str(nm))
            files.append(self._obj(str(nm), m))
        for e in self.cache.children(parts):              # not-yet-visible uploads
            if e["name"] not in seen and e["name"] not in names:
                files.append({"key": e["name"], "size": e["size"],
                              "etag": e["etag"], "modified": e["mtime"]})
        return names, files

    def find_media(self, parts):
        if not parts:
            return None
        parent = self.resolve_folder(list(parts[:-1]))
        if parent is not None:
            for m in self.c.list_media(parent):
                if str(first(m, "name", "filename")) == parts[-1]:
                    self.cache.drop(parts)
                    return m
        e = self.cache.get(parts)        # fall back to a just-uploaded file
        return _synth(e) if e else None

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
        parent = self.resolve_folder(list(parts[:-1]), create=True)
        self._overwrite(list(parts), parent, parts[-1])
        new_id = self.c.upload(parent, parts[-1], data, content_type)
        etag = hashlib.md5(data).hexdigest()
        self.cache.put(parts, len(data), etag, content_type, data, media_id=new_id)

    def make_dir(self, parts):
        self.resolve_folder(list(parts), create=True)
        self.invalidate()

    def delete_path(self, parts):
        if not parts:
            return
        fid = self.resolve_folder(list(parts))
        if fid is not None:
            self.c.delete_folder(fid)
            self.invalidate()
            return
        m = self.find_media(parts)
        self.cache.drop(parts)
        if m is not None:
            mid = first(m, "id", "mediaid")
            if mid:                       # cached items now carry the server id too
                self.c.delete_media(mid)

    def download_path(self, parts):
        m = self.find_media(parts)
        return None if m is None else self._download(m)
