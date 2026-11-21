#!/usr/bin/env python3
"""
funambridge -- accede a O2 Cloud (Funambol OneMediaHub) por S3 y WebDAV.

O2 Cloud (Telefónica España) es un despliegue white-label de Funambol
OneMediaHub con una API propietaria (SAPI) y login MobileConnect (teléfono +
SMS). No tiene S3, ni WebDAV, ni cliente Linux. funambridge hace de puente.

Subcomandos:
    serve        sirve el proxy S3 + WebDAV + panel web (por defecto)
    import-har   importa una sesión web desde un fichero HAR
    probe        prueba la autenticación de una cuenta
    dedupe       busca y repara copias duplicadas que dejaron subidas fallidas

La configuración vive en un YAML (por defecto: ./config.yaml).
"""

import argparse
import logging
import os
import sys
import threading

from . import config
from .sapi import FunambolClient, SapiError
from .s3 import Registry, Server

log = logging.getLogger("funambridge")
DEFAULT_CONFIG = os.environ.get("FUNAMBRIDGE_CONFIG", "config.yaml")


def _client(acc, cfg):
    return FunambolClient(acc.base_url, acc.device_id, oauth=acc.oauth,
                          session=acc.session,
                          on_token_refresh=lambda _o: cfg.save())


def _auth_kind(acc):
    if acc.oauth.is_set():
        return "tokens"
    if acc.session.is_set():
        return "session(HAR)"
    return "NONE"


def cmd_serve(cfg):
    registry = Registry(cfg)
    s3 = Server((cfg.s3_host, cfg.s3_port), registry,
                log=lambda m: log.info("%s", m))
    scheme = "http"
    if cfg.tls_cert and cfg.tls_key:
        from . import tls
        try:
            ctx = tls.server_context(cfg.tls_cert, cfg.tls_key)
            s3.socket = ctx.wrap_socket(s3.socket, server_side=True)
            scheme = "https"
            log.info("TLS activado (cert=%s)", cfg.tls_cert)
        except Exception as e:  # noqa: BLE001
            log.error("TLS deshabilitado (%s); sirviendo HTTP", e)
    base = f"{scheme}://{cfg.s3_host}:{cfg.s3_port}"
    if cfg.s3_host not in ("127.0.0.1", "localhost", "::1"):
        sig = "on" if cfg.verify_s3_signature else "OFF"
        log.warning("Binding %s -- reachable from your LAN/Docker network "
                    "(S3 signature check: %s). Put it behind TLS/your own auth on "
                    "untrusted networks.", cfg.s3_host, sig)
    log.info("funambridge on %s", base)
    log.info("  admin UI : %s/_admin/   (open in a browser)", base)
    log.info("  S3 API   : %s          (%d account(s))", base, len(cfg.accounts))
    for a in cfg.accounts:
        log.info("    '%s'  access_key=%s  auth=%s",
                 a.name, a.access_key, _auth_kind(a))
    if not cfg.accounts:
        log.info("  No accounts yet -- open %s/ to add one.", base)
    try:
        s3.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        s3.shutdown()


def cmd_import_har(cfg, path, name):
    from . import har
    try:
        data = har.parse_har(path)
        acc = cfg.upsert_from_har(data, name=name)
    except (OSError, ValueError) as e:
        sys.exit(f"could not import HAR: {e}")
    print(f"Imported session for account '{acc.name}'.")
    print(f"  access_key = {acc.access_key}")
    print(f"  secret_key = {acc.secret_key}")
    print(f"  validationkey: {'yes' if acc.session.validationkey else 'MISSING'}")
    print("Run:  python o2cloud_s3_proxy.py probe " + acc.name + " -v")


def cmd_probe(cfg, name):
    acc = cfg.by_name(name) if name else (cfg.accounts[0] if cfg.accounts else None)
    if acc is None:
        sys.exit("no account to probe")
    if not acc.has_auth():
        sys.exit(f"account '{acc.name}' has no auth -- import a HAR or add tokens")
    print(f"== probe '{acc.name}' ({acc.base_url}) ==")
    client = _client(acc, cfg)
    info = client.system_information()
    print("SAPI:", info.get("sapiversion"), "| dl base:", client.download_base)
    client.validate_session()
    print("session: VALID (tokens accepted)")
    from .store import Store
    store = Store(client, cache_ttl=acc.cache_seconds, root_bucket=cfg.root_bucket)
    print("buckets:", store.list_buckets() or "(none)")
    try:
        print("storage:", str(client.storage_space())[:160])
    except SapiError as e:
        print("storage: (skipped:", e, ")")


def cmd_dedupe(cfg, name, path, apply, har_path=None):
    from . import repair
    if har_path:
        from . import har
        d = har.parse_har(har_path)
        sess = config.Session(jsessionid=d["jsessionid"],
                              validationkey=d.get("validationkey", ""),
                              plc=d.get("plc", ""))
        client = FunambolClient(d.get("base_url") or config.DEFAULT_BASE_URL,
                                d.get("device_id") or "", session=sess)
    else:
        acc = cfg.by_name(name) if name else (cfg.accounts[0] if cfg.accounts
                                              else None)
        if acc is None or not acc.has_auth():
            sys.exit("no hay cuenta con sesion (o usa --har)")
        client = _client(acc, cfg)
    repair.run(client, path, apply=apply, out=lambda m: print(m, flush=True))


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001  (older Python / non-reconfigurable)
            pass
    p = argparse.ArgumentParser(
        prog="funambridge",
        description="Accede a O2 Cloud (Funambol OneMediaHub) por S3 y WebDAV")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="ruta a config.yaml")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG también por consola")
    p.add_argument("--log-file", default=None,
                   help="escribe un log extendido (DEBUG) a este fichero")
    sub = p.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", help="run the S3 proxy + admin web UI")
    sv.add_argument("--host", default=None,
                    help="bind address override (e.g. 0.0.0.0 to allow other "
                         "machines on your LAN). Default: config value / 127.0.0.1")
    sv.add_argument("--port", type=int, default=None, help="port override")
    sv.add_argument("--tls-cert", default=None,
                    help="cert PEM para servir HTTPS (autofirmado si no existe)")
    sv.add_argument("--tls-key", default=None, help="clave PEM para HTTPS")
    ih = sub.add_parser("import-har", help="import a web session from a HAR file")
    ih.add_argument("har")
    ih.add_argument("--name", default=None)
    pr = sub.add_parser("probe", help="test an account's auth")
    pr.add_argument("account", nargs="?")
    dd = sub.add_parser("dedupe", help="busca/repara copias duplicadas en O2 "
                                       "(simulación salvo --apply)")
    dd.add_argument("path", help="carpeta de O2, p. ej. backup/nas.hbk")
    dd.add_argument("--account", default=None)
    dd.add_argument("--har", default=None, help="usar la sesión de este HAR")
    dd.add_argument("--apply", action="store_true",
                    help="aplicar los cambios (si no, solo informa)")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = config.load(args.config)
    cfg.path = args.config

    # Extended on-disk log (DEBUG), rotating. Console stays at its level.
    log_file = (args.log_file or os.environ.get("FUNAMBRIDGE_LOG_FILE")
                or cfg.log_file)
    if log_file:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3,
                                 encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        flog = logging.getLogger("funambridge")
        flog.addHandler(fh)
        flog.setLevel(logging.DEBUG)
        log.info("Log extendido (DEBUG) -> %s", log_file)

    cmd = args.cmd or "serve"
    if cmd == "serve":
        if getattr(args, "host", None):
            cfg.s3_host = args.host
        if getattr(args, "port", None):
            cfg.s3_port = args.port
        if getattr(args, "tls_cert", None):
            cfg.tls_cert = args.tls_cert
        if getattr(args, "tls_key", None):
            cfg.tls_key = args.tls_key
        cmd_serve(cfg)
    elif cmd == "import-har":
        cmd_import_har(cfg, args.har, args.name)
    elif cmd == "probe":
        cmd_probe(cfg, args.account)
    elif cmd == "dedupe":
        cmd_dedupe(cfg, args.account, args.path, args.apply, args.har)


if __name__ == "__main__":
    main()
