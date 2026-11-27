"""
Maps WebDAV paths onto OneMediaHub folders/media for a single account
("a/b/c.txt" = sub-folders a, b + file c.txt, relative to the O2 root).

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
import re
import tempfile
import threading
import time

from .sapi import FunambolClient, SapiError, SessionExpired, first

log = logging.getLogger("funambridge.store")

# memory budget for cached file *content* (metadata is always kept while fresh);
# content above the remaining budget is spilled to disk instead.
_CONTENT_BUDGET = 256 * 1024 * 1024

# An incoming upload is read into RAM up to this size; bigger ones spill to a
# temp file on disk as they stream in, so a large PUT never sits in memory.
_SPOOL_MAX = 16 * 1024 * 1024

# O2 never overwrites: uploading a name that already exists stores the new item
# as "stem (N).ext". Used to recognise copies spawned by a failed upload.
_VARIANT_RE = re.compile(r"^(?P<stem>.*) \((?P<n>\d+)\)(?P<ext>\.[^.]*)?$")

# After an upload fails (timeout, reset, 5xx) O2 may still have stored it and
# only show it a while later. Sweep for such stray copies after these delays.
_STRAY_SWEEPS = (45, 150)


def canonical_name(name):
    """'a.bucket (2).3' -> 'a.bucket.3'; names without the suffix unchanged."""
    m = _VARIANT_RE.match(name)
    return (m.group("stem") + (m.group("ext") or "")) if m else name


def _ms(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


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
        # Background sweeper: expired entries (and their on-disk spill files) are
        # otherwise only reclaimed when something touches that path again, so a
        # write-once file would leak its .bin until restart. This drops them as
        # soon as they go stale.
        self._stop = threading.Event()
        self._sweeper = None
        if self.ttl > 0:
            self._sweeper = threading.Thread(
                target=self._sweep_loop, name="fb-cache-sweeper", daemon=True)
            self._sweeper.start()

    def close(self):
        self._stop.set()

    def _sweep_loop(self):
        interval = max(1.0, min(self.ttl, 10))
        while not self._stop.wait(interval):
            try:
                self._sweep()
            except Exception:  # noqa: BLE001  (never let the sweeper die)
                pass

    def _sweep(self):
        """Drop every expired entry (and delete its spill file)."""
        with self._lock:
            for k in [k for k, e in self._e.items() if not self._fresh(e)]:
                self._drop(k)

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
        """Current entries for the admin view: path, size, where, expiry (s).
        Sweeps expired entries first, so the panel reflects what's really on
        disk."""
        self._sweep()
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
    __slots__ = ("ctype", "size", "etag", "data", "path", "opener")

    def __init__(self, ctype, size, etag="", data=None, path=None, opener=None):
        self.ctype = ctype
        self.size = int(size)
        self.etag = etag or ""
        self.data = data
        self.path = path
        # opener(start, end) -> Stream of those bytes straight from O2 (nothing
        # is buffered: the bytes are relayed to the client as they arrive).
        self.opener = opener


class Store:
    def __init__(self, client: FunambolClient, cache_ttl=0, disk_dir=None):
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
        # (folder_id, name) -> ms timestamp of an upload that failed after it
        # may have reached O2 (see _mark_dirty).
        self._dirty = {}
        self._closed = threading.Event()
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

    def close(self):
        """Stop the cache sweeper (called when this Store is discarded)."""
        self._closed.set()
        self.cache.close()

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
                ent = {
                    "id": str(mid),
                    "size": int(first(m, "size", "filesize") or 0),
                    "modified": first(m, "modificationdate", "creationdate"),
                    "etag": str(first(m, "etag", "id") or ""),
                }
                prev = loaded.get(str(nm))
                if prev is not None:
                    # Same name twice (left by an old failed upload): serve the
                    # newest, remember the rest so overwrite/delete remove them.
                    if _ms(ent["modified"]) >= _ms(prev["modified"]):
                        prev, ent = ent, prev
                    prev.setdefault("dups", []).append(ent["id"])
                    prev["dups"].extend(ent.pop("dups", []))
                    ent = prev
                loaded[str(nm)] = ent
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

    def _index_items(self, folder_id):
        """A consistent snapshot [(name, entry), ...] of a folder's index, taken
        under the lock so a concurrent upload/delete can't mutate the dict while
        a listing iterates it."""
        idx = self._media_index_for(folder_id)     # ensure loaded (may do I/O)
        with self._mi_lock:
            return list(idx.items())

    @staticmethod
    def _item_from_index(name, ent):
        """A media-item-like dict built from an index entry (for reads)."""
        return {"id": ent["id"], "name": name, "size": ent["size"],
                "modificationdate": ent.get("modified"),
                "etag": ent.get("etag", ""), "dups": list(ent.get("dups", []))}

    def _put_lock(self, folder_id, name):
        # Same (folder, name) always maps to the same lock, so two concurrent
        # PUTs of one file can't both create it (O2 would keep 'name' AND
        # 'name (1)'). The fixed pool bounds memory.
        return self._put_locks[hash((str(folder_id), name)) % len(self._put_locks)]

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
        ids = []
        if idx is not None:
            with self._mi_lock:
                ent = idx.get(fname)
            if ent:
                ids = [ent["id"]] + list(ent.get("dups", []))
        if self._is_dirty(folder_id, fname):
            # a previous upload of this name failed: O2 may hold copies we
            # never learnt the id of -- look them up fresh.
            try:
                for m in self.c.list_media(folder_id):
                    mid = first(m, "id", "mediaid")
                    if str(first(m, "name", "filename")) == fname and mid:
                        if str(mid) not in ids:
                            ids.append(str(mid))
            except SapiError as ex:
                log.debug("overwrite: fresh list %s: %s", folder_id, ex)
        for mid in ids:
            try:
                self.c.delete_media(mid)
            except SapiError as ex:
                log.debug("overwrite: delete %s (%s): %s", "/".join(parts), mid, ex)
        if ids:
            self._index_pop(folder_id, fname)
        self.cache.drop(parts)

    # -- stray copies left by failed uploads --------------------------------
    def _is_dirty(self, folder_id, fname):
        with self._mi_lock:
            return (str(folder_id), fname) in self._dirty

    def _mark_dirty(self, folder_id, fname):
        """An upload of fname failed after (maybe) reaching O2. Remember it and
        schedule sweeps that delete any copy the server surfaces later."""
        key = (str(folder_id), fname)
        with self._mi_lock:
            first_time = key not in self._dirty
            self._dirty.setdefault(key, int(time.time() * 1000) - 60_000)
        if not first_time:
            return

        def run():
            for delay in _STRAY_SWEEPS:
                if self._closed.wait(delay):
                    return
                try:
                    self._sweep_strays(folder_id, fname)
                except Exception as ex:  # noqa: BLE001
                    log.debug("stray sweep %s/%s: %s", folder_id, fname, ex)
            with self._mi_lock:
                self._dirty.pop(key, None)
        threading.Thread(target=run, daemon=True,
                         name="funambridge-stray").start()

    def _sweep_strays(self, folder_id, fname):
        """Delete copies of fname other than the one the index serves: same
        name with another id, or an O2-renamed 'stem (N).ext' created after the
        failed upload. Holds the name's PUT lock so an upload in flight (whose
        id we don't know yet) is never mistaken for a stray."""
        key = (str(folder_id), fname)
        with self._put_lock(folder_id, fname):
            with self._mi_lock:
                since = self._dirty.get(key)
                idx = self._media_index.get(str(folder_id))
                ent = idx.get(fname) if idx is not None else None
                keep = ent["id"] if ent else None
            if since is None:
                return
            items = self.c.list_media(folder_id)
            name_of = {str(first(m, "id", "mediaid")): str(first(m, "name", "filename"))
                       for m in items}
            if keep and keep in name_of and name_of[keep] != fname:
                # The retry landed while the stray still existed, so O2 stored
                # it renamed ('stem (1).ext'). The stray holds the same bytes
                # (same file, same size): serve that one under the real name.
                same = [m for m in items
                        if str(first(m, "name", "filename")) == fname
                        and int(first(m, "size", "filesize") or 0) == ent["size"]]
                if same:
                    best = max(same, key=lambda m: _ms(first(
                        m, "modificationdate", "creationdate")))
                    with self._mi_lock:
                        ent["id"] = str(first(best, "id", "mediaid"))
                    keep = ent["id"]
                else:
                    log.warning("'%s' quedó guardado en O2 como '%s'",
                                fname, name_of[keep])
            removed = []
            for m in items:
                nm = str(first(m, "name", "filename") or "")
                mid = str(first(m, "id", "mediaid") or "")
                if not mid or mid == keep:
                    continue
                born = _ms(first(m, "creationdate", "modificationdate"))
                if nm == fname or (canonical_name(nm) == fname and born >= since):
                    try:
                        self.c.delete_media(mid)
                        removed.append(nm)
                    except SapiError as ex:
                        log.debug("stray delete %s (%s): %s", nm, mid, ex)
            if removed:
                log.info("borradas %d copias sobrantes de '%s' tras una subida "
                         "fallida: %s", len(removed), fname, ", ".join(removed))
            if ent and ent.get("dups"):
                with self._mi_lock:
                    ent.pop("dups", None)

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
                try:
                    if data is not None:
                        new_id = self.c.upload(fid, fname, total, data,
                                               content_type)
                    else:
                        with open(path, "rb") as fh:
                            new_id = self.c.upload(fid, fname, total, fh,
                                                   content_type)
                except SessionExpired:
                    raise                 # rejected up front: nothing stored
                except Exception:
                    # Timeout/reset/5xx: O2 may have stored it anyway. Hunt the
                    # stray copies down so the client's retry can't duplicate.
                    self._mark_dirty(fid, fname)
                    raise
                self._index_set(fid, fname, new_id, total,
                                int(time.time() * 1000), md5)
                if data is not None:
                    self.cache.put(parts, total, md5, content_type, data,
                                   media_id=new_id)
                else:
                    adopted = self.cache.put_path(parts, total, md5, content_type,
                                                  path, media_id=new_id)
            return md5
        finally:
            if path is not None and not adopted:
                try:
                    os.remove(path)
                except OSError:
                    pass

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
        for nm, ent in self._index_items(fid):
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

    def browse(self, parts, limit=500):
        """Fresh, read-only listing straight from the server, bypassing the
        per-folder index and the write cache. Used by the admin explorer, so a
        successful answer really proves the account's credentials still work."""
        fid = self.resolve_folder(list(parts))
        if fid is None:
            raise KeyError("/".join(parts))
        folders = sorted((str(first(f, "name", "foldername"))
                          for f in self.c.list_folders(fid)
                          if first(f, "name", "foldername") is not None),
                         key=str.lower)
        files = []
        for m in self.c.list_media(fid):
            nm = first(m, "name", "filename")
            if nm is None:
                continue
            files.append({"name": str(nm),
                          "size": int(first(m, "size", "filesize") or 0),
                          "modified": first(m, "modificationdate", "creationdate")})
        files.sort(key=lambda x: x["name"].lower())
        return {"folders": folders, "files": files[:limit],
                "total_files": len(files)}

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
        mid = first(m, "id", "mediaid")
        size = int(first(m, "size", "filesize") or 0)
        name = str(first(m, "name", "filename") or "")
        ctype = mimetypes.guess_type(name)[0] or ctype
        return Download(ctype, size, etag=str(first(m, "etag") or mid or ""),
                        opener=lambda a, b, _id=mid: self.c.open_media(_id, a, b))

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
            self._delete_folder(fid, "/".join(parts))
            self.invalidate()
            return
        m = self.find_media(parts)
        self.cache.drop(parts)
        if m is not None:
            mid = first(m, "id", "mediaid")
            if mid:                       # cached items now carry the server id too
                self.c.delete_media(mid)
            for dup in m.get("dups", []):     # leftover same-name copies
                try:
                    self.c.delete_media(dup)
                except SapiError as ex:
                    log.debug("delete dup %s: %s", dup, ex)
            parent = self.resolve_folder(list(parts[:-1]))
            if parent is not None:
                self._index_pop(parent, parts[-1])

    def _delete_folder(self, fid, label):
        """Soft-delete a folder. O2 refuses to do it in one go when it holds
        more items than folders.softdelete-updates-limit (FOL-1036, 1000 in
        Nov 2026): then empty it first -- files one by one, sub-folders
        recursively -- and delete the (now small) folder."""
        try:
            self.c.delete_folder(fid)
        except SapiError as e:
            if e.code != "FOL-1036" and "FOL-1036" not in str(e):
                raise
            log.info("'%s' tiene demasiados elementos para borrarla de una vez "
                     "(FOL-1036); se vacía por partes", label)
            for f in self.c.list_folders(fid):
                sub = first(f, "id", "folderid")
                if sub is not None:
                    self._delete_folder(str(sub), label + "/" +
                                        str(first(f, "name", "foldername")))
            for m in self.c.list_media(fid):
                mid = first(m, "id", "mediaid")
                if mid:
                    self.c.delete_media(mid)
            self.c.delete_folder(fid)
        with self._mi_lock:
            self._media_index.pop(str(fid), None)   # folder gone

    def stat_path(self, parts):
        """Metadata of a file (for HEAD) without touching its content."""
        m = self.find_media(parts)
        if m is None:
            return None
        name = str(first(m, "name", "filename") or (parts[-1] if parts else ""))
        return {"size": int(first(m, "size", "filesize") or 0),
                "etag": str(first(m, "etag", "id") or ""),
                "modified": first(m, "modificationdate", "creationdate"),
                "ctype": mimetypes.guess_type(name)[0] or "application/octet-stream"}

    def quota(self, max_age=60):
        """(used, available) bytes of the account, cached briefly (WebDAV
        quota properties are asked on every PROPFIND)."""
        now = time.time()
        q = getattr(self, "_quota", None)
        if q and now - q[2] < max_age:
            return q[0], q[1]
        d = self.c.storage_space() or {}
        used = int(d.get("used") or 0)
        free = d.get("free")
        if free is None and d.get("quota") is not None:
            free = int(d["quota"]) - used
        free = max(0, int(free or 0))
        self._quota = (used, free, now)
        return used, free

    def download_path(self, parts):
        m = self.find_media(parts)
        return None if m is None else self._download(m)
