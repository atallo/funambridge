"""
Configuration model for funambridge (token edition).

Stored as YAML. Holds global S3/admin settings plus one or more O2 Cloud
accounts. Each account has:
  * its own auto-generated S3 access_key/secret_key (UUID-based), so an S3
    client selects the account by which key pair it signs with;
  * an OAuth credential captured from a browser login (see the admin page).

The OAuth credential mirrors what the official Android client stores and sends
to SAPI as `Authorization: oauth <base64(...)>`. It auto-refreshes: the server
returns a new token in each response, which the SAPI client persists back here.
"""

import base64
import datetime as _dt
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict

import yaml

DEFAULT_BASE_URL = "https://cloud.o2online.es"


def new_key():
    return "O2" + uuid.uuid4().hex[:18].upper()


def new_secret():
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class OAuth:
    accesstoken: str = ""
    refreshtoken: str = ""
    platform: str = "web"
    expiresin: str = ""
    lastrefreshdate: int = 0
    msisdn: str = ""
    captured_at: str = ""

    def is_set(self):
        return bool(self.accesstoken or self.refreshtoken)


@dataclass
class Session:
    """Web-login session (cookie auth), e.g. captured from a HAR.

    O2 auth needs three cookies (JSESSIONID, validationKey, PLC) plus the
    validationkey echoed as a query param (CSRF double-submit)."""
    jsessionid: str = ""
    validationkey: str = ""
    plc: str = ""
    captured_at: str = ""

    def is_set(self):
        return bool(self.jsessionid and self.validationkey)


@dataclass
class Account:
    name: str
    base_url: str = DEFAULT_BASE_URL
    access_key: str = field(default_factory=new_key)
    secret_key: str = field(default_factory=new_secret)
    device_id: str = field(default_factory=lambda: "funambridge-" + uuid.uuid4().hex[:12])
    s3_enabled: bool = True
    webdav_enabled: bool = True
    oauth: OAuth = field(default_factory=OAuth)
    session: Session = field(default_factory=Session)

    def has_auth(self):
        return self.oauth.is_set() or self.session.is_set()

    def set_keys(self, access_key=None, secret_key=None):
        """Set keys to given values, or generate fresh random ones when blank."""
        self.access_key = (access_key or "").strip() or new_key()
        self.secret_key = (secret_key or "").strip() or new_secret()

    @property
    def phone(self):
        return self.oauth.msisdn

    def set_session(self, jsessionid, validationkey="", plc=""):
        self.session = Session(jsessionid=jsessionid,
                               validationkey=validationkey or "",
                               plc=plc or "", captured_at=_now())

    def set_tokens(self, access_token, refresh_token, expires_in="",
                   msisdn="", platform="web"):
        self.oauth.accesstoken = access_token or self.oauth.accesstoken
        self.oauth.refreshtoken = refresh_token or self.oauth.refreshtoken
        if expires_in:
            self.oauth.expiresin = str(expires_in)
        if msisdn:
            self.oauth.msisdn = msisdn
        if platform:
            self.oauth.platform = platform
        self.oauth.lastrefreshdate = int(time.time() * 1000)
        self.oauth.captured_at = _now()


@dataclass
class Config:
    path: str
    s3_host: str = "0.0.0.0"   # reachable from the LAN by default (see warning on serve)
    s3_port: int = 9000
    admin_enabled: bool = True
    verify_s3_signature: bool = True
    accounts: list = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- lookups ------------------------------------------------------------
    def by_access_key(self, k):
        return next((a for a in self.accounts if a.access_key == k), None)

    def by_name(self, n):
        return next((a for a in self.accounts if a.name == n), None)

    def add_account(self, name, base_url=DEFAULT_BASE_URL):
        acc = Account(name=name, base_url=base_url)
        self.accounts.append(acc)
        return acc

    def remove_account(self, name):
        self.accounts = [a for a in self.accounts if a.name != name]

    def upsert_from_blob(self, b64, name=None):
        """Decode the base64 produced by the browser console snippet and create
        or update the matching account. Returns the Account."""
        try:
            raw = base64.b64decode(b64, validate=False)
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"invalid credential blob: {e}")
        access = data.get("access_token") or data.get("accesstoken")
        refresh = data.get("refresh_token") or data.get("refreshtoken")
        msisdn = data.get("msisdn", "")
        if not (access or refresh):
            raise ValueError("blob has no access_token/refresh_token")
        name = name or data.get("name") or (f"o2-{msisdn}" if msisdn else "o2")
        acc = self.by_name(name) or self.add_account(name)
        acc.set_tokens(access, refresh,
                       expires_in=data.get("expires_in") or data.get("expiresin", ""),
                       msisdn=msisdn,
                       platform=data.get("platform", "web"))
        self.save()
        return acc

    def upsert_session(self, name, jsessionid, validationkey="", plc="",
                       base_url=None):
        """Create/update an account from a manually-provided web session."""
        jsessionid = (jsessionid or "").strip()
        if jsessionid.lower().startswith("jsessionid="):
            jsessionid = jsessionid.split("=", 1)[1]
        validationkey = (validationkey or "").strip()
        if not jsessionid:
            raise ValueError("JSESSIONID vacio")
        if not validationkey:
            raise ValueError("validationkey vacio (necesario para autenticar)")
        acc = self.by_name(name) or self.add_account(name or "o2")
        if base_url:
            acc.base_url = base_url
        acc.set_session(jsessionid, validationkey, (plc or "").strip())
        self.save()
        return acc

    def upsert_from_har(self, parsed, name=None):
        """Create/update an account from a parsed HAR (har.parse_har*).
        Returns the Account. Raises ValueError if no session was found."""
        jid = (parsed or {}).get("jsessionid")
        if not jid:
            raise ValueError("no JSESSIONID found in the HAR (make sure it "
                             "captures a logged-in session at cloud.o2online.es)")
        if not (parsed or {}).get("validationkey"):
            raise ValueError("no validationKey found in the HAR (capture it AFTER "
                             "you are fully logged in)")
        msisdn = parsed.get("msisdn", "")
        name = name or (f"o2-{msisdn}" if msisdn else "o2")
        acc = self.by_name(name) or self.add_account(name)
        if parsed.get("base_url"):
            acc.base_url = parsed["base_url"]
        if msisdn:
            acc.oauth.msisdn = msisdn
        acc.set_session(jid, parsed.get("validationkey", ""), parsed.get("plc", ""))
        self.save()
        return acc

    # -- persistence --------------------------------------------------------
    def to_dict(self):
        return {
            "s3": {"host": self.s3_host, "port": self.s3_port},
            "admin": {"enabled": self.admin_enabled},
            "verify_s3_signature": self.verify_s3_signature,
            "accounts": [{
                "name": a.name,
                "base_url": a.base_url,
                "access_key": a.access_key,
                "secret_key": a.secret_key,
                "device_id": a.device_id,
                "s3_enabled": a.s3_enabled,
                "webdav_enabled": a.webdav_enabled,
                "oauth": asdict(a.oauth),
                "session": asdict(a.session),
            } for a in self.accounts],
        }

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump(self.to_dict(), fh, allow_unicode=True,
                               sort_keys=False)
            os.replace(tmp, self.path)


def load(path):
    cfg = Config(path=path)
    if not os.path.exists(path):
        return cfg
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    s3 = data.get("s3") or {}
    cfg.s3_host = s3.get("host", cfg.s3_host)
    cfg.s3_port = int(s3.get("port", cfg.s3_port))
    cfg.admin_enabled = bool((data.get("admin") or {}).get("enabled", True))
    cfg.verify_s3_signature = bool(data.get("verify_s3_signature", True))
    for a in data.get("accounts") or []:
        o = a.get("oauth") or {}
        cfg.accounts.append(Account(
            name=a.get("name", "account"),
            base_url=a.get("base_url", DEFAULT_BASE_URL),
            access_key=a.get("access_key") or new_key(),
            secret_key=a.get("secret_key") or new_secret(),
            device_id=a.get("device_id") or ("funambridge-" + uuid.uuid4().hex[:12]),
            s3_enabled=bool(a.get("s3_enabled", True)),
            webdav_enabled=bool(a.get("webdav_enabled", True)),
            oauth=OAuth(
                accesstoken=o.get("accesstoken", ""),
                refreshtoken=o.get("refreshtoken", ""),
                platform=o.get("platform", "web"),
                expiresin=o.get("expiresin", ""),
                lastrefreshdate=int(o.get("lastrefreshdate", 0) or 0),
                msisdn=o.get("msisdn", ""),
                captured_at=o.get("captured_at", ""),
            ),
            session=Session(
                jsessionid=(a.get("session") or {}).get("jsessionid", ""),
                validationkey=(a.get("session") or {}).get("validationkey", ""),
                plc=(a.get("session") or {}).get("plc", ""),
                captured_at=(a.get("session") or {}).get("captured_at", ""),
            ),
        ))
    return cfg
