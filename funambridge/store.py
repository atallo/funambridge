"""
Maps S3 (bucket, key) onto OneMediaHub folders/media for a single account.

  bucket          = a folder directly under the account root
  key "a/b/c.txt" = sub-folders a, b + file c.txt inside `bucket`
"""

import hashlib
import mimetypes
import threading

from .sapi import FunambolClient, first


class Store:
    def __init__(self, client: FunambolClient):
        self.c = client
        self._lock = threading.RLock()
        self._folder_cache = {}

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
        return sorted(self._children_folders(self.c.root_id()).keys())

    def create_bucket(self, name):
        self.resolve_folder([name], create=True)
        self.invalidate()

    def delete_bucket(self, name):
        fid = self.resolve_folder([name])
        if fid is None:
            raise KeyError(name)
        self.c.delete_folder(fid)
        self.invalidate()

    # -- objects ------------------------------------------------------------
    @staticmethod
    def _split_key(key):
        parts = [p for p in key.split("/") if p != ""]
        if not parts:
            return [], None
        return parts[:-1], parts[-1]

    def list_objects(self, bucket, prefix="", delimiter=""):
        fid = self.resolve_folder([bucket])
        if fid is None:
            raise KeyError(bucket)
        pdirs = [p for p in prefix.split("/")[:-1] if p]
        base_id = self.resolve_folder([bucket] + pdirs)
        objects, common = [], set()
        if base_id is None:
            return objects, sorted(common)
        base_prefix = "/".join(pdirs) + ("/" if pdirs else "")
        for m in self.c.list_media(base_id):
            name = first(m, "name", "filename")
            if name is None:
                continue
            full = base_prefix + str(name)
            if not full.startswith(prefix):
                continue
            objects.append({
                "key": full,
                "size": int(first(m, "size", "filesize") or 0),
                "etag": str(first(m, "etag", "id") or ""),
                "modified": first(m, "modificationdate", "lastupdate",
                                  "creationdate"),
            })
        if delimiter == "/":
            for sub in self._children_folders(base_id):
                cp = base_prefix + sub + "/"
                if cp.startswith(prefix) or prefix.startswith(cp):
                    common.add(cp)
        return objects, sorted(common)

    def _find_item(self, bucket, key):
        dirs, fname = self._split_key(key)
        fid = self.resolve_folder([bucket] + dirs)
        if fid is None:
            return None
        for m in self.c.list_media(fid):
            if str(first(m, "name", "filename")) == fname:
                return m
        return None

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
        return self.c.download(m)

    def put_object(self, bucket, key, data, content_type):
        dirs, fname = self._split_key(key)
        if fname is None:
            raise ValueError("empty key")
        fid = self.resolve_folder([bucket] + dirs, create=True)
        self.c.upload(fid, fname, data, content_type)
        return hashlib.md5(data).hexdigest()

    def delete_object(self, bucket, key):
        m = self._find_item(bucket, key)
        if m is not None:
            self.c.delete_media(first(m, "id", "mediaid"))

    # -- path-based API (used by WebDAV; parts are relative to the O2 root,
    #    so the root itself can hold files, not just folders) ----------------
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
        files = []
        for m in self.c.list_media(fid):
            nm = first(m, "name", "filename")
            if nm is not None:
                files.append(self._obj(str(nm), m))
        return names, files

    def find_media(self, parts):
        if not parts:
            return None
        parent = self.resolve_folder(list(parts[:-1]))
        if parent is None:
            return None
        for m in self.c.list_media(parent):
            if str(first(m, "name", "filename")) == parts[-1]:
                return m
        return None

    def put_file(self, parts, data, content_type):
        if not parts:
            raise ValueError("no file name")
        parent = self.resolve_folder(list(parts[:-1]), create=True)
        self.c.upload(parent, parts[-1], data, content_type)

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
        if m is not None:
            self.c.delete_media(first(m, "id", "mediaid"))

    def download_path(self, parts):
        m = self.find_media(parts)
        return None if m is None else self.c.download(m)
