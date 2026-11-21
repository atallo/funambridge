"""
Repair of duplicate copies left on O2 by failed uploads.

O2 never overwrites: an upload whose name already exists is stored as
'stem (N).ext', and an upload that timed out on our side may still have been
stored. Older funambridge versions (before 0.1.10) retried such uploads and
left extra copies: the same name twice, or 'name (N).ext' next to 'name'.
Backup tools then see the wrong copy or extra files and fail their integrity
check.

For every group (a name plus its other copies) this downloads every copy,
checks it is complete (real length == size) and compares contents (MD5):

  * all complete copies identical -> keep one under the real name, move the
    rest to the trash (soft delete);
  * only an 'name (N)' copy is complete -> upload its content under the real
    name, then trash the others;
  * complete copies that differ -> nothing is touched, reported for review.

Without apply=True nothing is changed (dry run).
"""

import hashlib
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from .sapi import SapiError, first
from .store import _ms, canonical_name

log = logging.getLogger("funambridge.repair")


def _fmt_ms(v):
    v = _ms(v)
    if not v:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(v / 1000))


def _gb(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


def _resolve(client, path):
    fid = client.root_id()
    for name in [p for p in path.split("/") if p]:
        kids = {str(first(f, "name", "foldername")): str(first(f, "id", "folderid"))
                for f in client.list_folders(fid)}
        if name not in kids:
            raise KeyError(path)
        fid = kids[name]
    return fid


def walk(client, path):
    """Yield (path, folder_id, media_items) for path and all its sub-folders."""
    stack = [(path.strip("/"), _resolve(client, path))]
    while stack:
        p, fid = stack.pop()
        yield p, fid, client.list_media(fid)
        for f in sorted(client.list_folders(fid),
                        key=lambda f: str(first(f, "name", "foldername")),
                        reverse=True):
            nm = str(first(f, "name", "foldername"))
            stack.append((f"{p}/{nm}" if p else nm, str(first(f, "id", "folderid"))))


def find_groups(items):
    """Groups of copies in one folder: {real_name: [item, ...]} with more than
    one copy, plus the 'name (N)' items whose real name does not exist."""
    by_name = {}
    for m in items:
        nm = first(m, "name", "filename")
        if nm is not None:
            by_name.setdefault(str(nm), []).append(m)
    groups, orphans = {}, []
    for nm, ms in by_name.items():
        canon = canonical_name(nm)
        if canon != nm and canon not in by_name:
            orphans.extend(ms)
            continue
        groups.setdefault(canon, []).extend(ms)
    return {k: v for k, v in groups.items() if len(v) > 1}, orphans


def _digest(client, mid, keep_path=None):
    """(bytes, md5) of a media item, streamed; optionally saved to keep_path."""
    h, n = hashlib.md5(), 0
    out = open(keep_path, "wb") if keep_path else None
    try:
        with client.open_download(mid) as resp:
            while True:
                b = resp.read(1 << 20)
                if not b:
                    break
                h.update(b)
                n += len(b)
                if out:
                    out.write(b)
    finally:
        if out:
            out.close()
    return n, h.hexdigest()


def plan_group(client, canon, copies, workers=4):
    """Download every copy of a group and decide what to do with it."""
    rows = []
    for m in copies:
        rows.append({"id": str(first(m, "id", "mediaid")),
                     "name": str(first(m, "name", "filename")),
                     "size": int(first(m, "size", "filesize") or 0),
                     "date": _ms(first(m, "modificationdate", "creationdate"))})

    def check(r):
        try:
            r["got"], r["md5"] = _digest(client, r["id"])
        except (SapiError, OSError) as ex:
            r["got"], r["md5"], r["err"] = -1, None, str(ex)
        r["ok"] = r["got"] == r["size"]
        return r

    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(check, rows))
    good = [r for r in rows if r["ok"]]
    plan = {"name": canon, "copies": rows, "keep": None, "delete": [],
            "reupload_from": None, "problem": None}
    if not good:
        plan["problem"] = "ninguna copia está completa"
        return plan
    if len({r["md5"] for r in good}) > 1:
        plan["problem"] = "hay copias completas con contenido distinto"
        return plan
    exact = [r for r in good if r["name"] == canon]
    if exact:
        keep = max(exact, key=lambda r: (r["date"], int(r["id"])))
    else:
        keep = None
        plan["reupload_from"] = max(good, key=lambda r: (r["date"], int(r["id"])))
    plan["keep"] = keep
    plan["delete"] = [r for r in rows if r is not keep]
    return plan


def apply_plan(client, folder_id, plan, tmp_dir=None):
    """Carry out a plan from plan_group (no-op for plans with a problem)."""
    if plan["problem"]:
        return False
    src = plan["reupload_from"]
    if src is not None:
        fd, path = tempfile.mkstemp(prefix="funambridge-fix-", dir=tmp_dir)
        os.close(fd)
        try:
            n, md5 = _digest(client, src["id"], keep_path=path)
            if n != src["size"] or md5 != src["md5"]:
                raise SapiError(f"{src['name']}: la copia cambió al releerla")
            # free the real name first, or O2 would store it as 'name (N)'
            for r in plan["delete"]:
                if r["name"] == plan["name"]:
                    client.delete_media(r["id"])
            with open(path, "rb") as fh:
                new_id = client.upload(folder_id, plan["name"], n, fh)
            plan["keep"] = {"id": new_id, "name": plan["name"], "size": n,
                            "md5": md5, "date": int(time.time() * 1000)}
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    for r in plan["delete"]:
        if src is not None and r["name"] == plan["name"]:
            continue                       # already deleted above
        client.delete_media(r["id"])
    return True


def run(client, path, apply=False, out=print, workers=4):
    """Scan `path` recursively, verify every duplicate group and (with
    apply=True) repair it. Returns a summary dict."""
    summary = {"folders": 0, "files": 0, "groups": 0, "fixed": 0,
               "problems": 0, "extra_bytes": 0, "orphans": 0}
    todo = []
    for p, fid, items in walk(client, path):
        summary["folders"] += 1
        summary["files"] += len(items)
        groups, orphans = find_groups(items)
        for m in orphans:
            summary["orphans"] += 1
            out(f"  ? {p}/{first(m, 'name', 'filename')}: copia '(N)' sin el "
                f"nombre original (no se toca)")
        for canon, copies in sorted(groups.items()):
            todo.append((p, fid, canon, copies))
    out(f"{summary['folders']} carpetas, {summary['files']} ficheros, "
        f"{len(todo)} grupos con copias de más")
    for p, fid, canon, copies in todo:
        summary["groups"] += 1
        plan = plan_group(client, canon, copies, workers=workers)
        out(f"\n{p}/{canon}")
        for r in sorted(plan["copies"], key=lambda r: (r["date"], int(r["id"]))):
            if plan["keep"] is r:
                tag = "CONSERVAR"
            elif plan["reupload_from"] is r:
                tag = "resubir como original, luego borrar"
            elif r in plan["delete"]:
                tag = "borrar"
            else:
                tag = "-"
            state = ("completa" if r["ok"] else
                     f"INCOMPLETA ({r['got']} de {r['size']} B)" if r["got"] >= 0
                     else f"ERROR ({r.get('err')})")
            out(f"    {r['name']:<34} {_gb(r['size']):>9}  {_fmt_ms(r['date'])}  "
                f"md5 {str(r['md5'])[:12]}  {state:<10}  -> {tag}")
        if plan["problem"]:
            summary["problems"] += 1
            out(f"    !! {plan['problem']}: no se toca")
            continue
        summary["extra_bytes"] += sum(r["size"] for r in plan["delete"])
        if apply:
            try:
                apply_plan(client, fid, plan)
                summary["fixed"] += 1
                out("    reparado")
            except (SapiError, OSError) as ex:
                summary["problems"] += 1
                out(f"    !! falló la reparación: {ex}")
    out(f"\nGrupos: {summary['groups']}, con problemas: {summary['problems']}, "
        f"espacio sobrante: {_gb(summary['extra_bytes'])}"
        + (f", reparados: {summary['fixed']}" if apply else
           "  (simulación: no se ha cambiado nada; usa --apply)"))
    return summary
