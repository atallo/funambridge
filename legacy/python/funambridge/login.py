"""
Login "normal" en O2 Cloud (número + SMS), sin HAR ni navegador.

Desde nov. 2026 la web de O2 usa OAuth2 + PKCE contra Telefónica. Este módulo
recorre ese mismo flujo por HTTP:

  1. GET  {base}/                         -> JSESSIONID previo (O2 guarda ahí el
                                             PKCE verifier de este login)
  2. GET  {base}/sapi/oauth/pkce/authorize?platform=web&deviceid=<id>
        -> 302 apiseg.telefonica.es (authorize, PKCE S256)
        -> 302 t3.o2online.es/acceso/#/...?sessionID=..&sessionData=..&state=..
  3. POST api.telefonica.es/.../manageCredentialMobileO2
        {mobile, sessionID, sessionData, consumerId}     -> envía el SMS
        <- {newSessionData, newSessionID}
  4. POST .../verifyCredentialMobileO2
        {otp, sessionData, sessionID, Mobile}            <- {redirectUri}
  5. GET  redirectUri = {base}/sapi/login/oauth?code=..&state=.. (mismo jar)
        <- Set-Cookie JSESSIONID + validationKey  => sesión lista

La sesión pertenece al `deviceid` del paso 2: ese es el X-deviceid que hay que
enviar después (el host de subidas lo exige). El API de Telefónica no usa
cookies: todo el estado viaja en sessionID/sessionData.
"""

import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

T3_API = "https://api.telefonica.es/t3/cus/segu/v5/seguCredentialO2s/"
CONSUMER_ID = "O2CLOUD_WEB"
COCO_ORIGIN = "O2CLOUD_WEB/loginAppO2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


class LoginError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # The login page carries its parameters in the URL #fragment, so every
    # Location has to be read by hand instead of being followed.
    def redirect_request(self, *a, **k):
        return None


def normalize_phone(s):
    """'+34 600 11 22 33' / '0034…' / '600112233' -> '600112233'."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 13 and d.startswith("0034"):
        d = d[4:]
    elif len(d) == 11 and d.startswith("34"):
        d = d[2:]
    if len(d) != 9:
        raise LoginError("el número debe tener 9 cifras")
    return d


def _qs(s):
    """Parse a query string like the web app does (decodeURIComponent: a '+'
    stays a '+', unlike urllib's parse_qs)."""
    out = {}
    for part in s.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
    return out


def _err(d):
    if isinstance(d, dict):
        for k in ("message", "description", "error_description", "error", "code"):
            if d.get(k):
                return ": " + str(d[k])[:120]
    return ""


class O2Login:
    """One in-progress login: start(phone) sends the SMS, verify(code) returns
    the new web session. Keep the same instance between both steps."""

    def __init__(self, base_url="https://cloud.o2online.es", device_id=None,
                 t3_api=None, timeout=30):
        self.base = base_url.rstrip("/")
        self.t3 = t3_api or T3_API
        self.timeout = timeout
        self.device_id = device_id or ("web-" + uuid.uuid4().hex)
        self.phone = None
        self._jar = http.cookiejar.CookieJar()
        self._op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar), _NoRedirect)
        self._gen = str(uuid.uuid4())       # COCO.idGeneral: one per login
        self._sid = self._sdata = None

    # -- http ----------------------------------------------------------------
    def _req(self, url, data=None, headers=None, method=None):
        h = {"User-Agent": UA}
        h.update(headers or {})
        try:
            r = self._op.open(urllib.request.Request(url, data=data, headers=h,
                                                     method=method),
                              timeout=self.timeout)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:          # 3xx land here too
            return e.code, e.headers, e.read()
        except urllib.error.URLError as e:
            raise LoginError("no se pudo conectar con %s: %s"
                             % (urllib.parse.urlparse(url).netloc, e.reason))

    def _t3(self, op, payload):
        hdrs = {"Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://t3.o2online.es",
                "Referer": "https://t3.o2online.es/",
                "COCO.idOrigen": COCO_ORIGIN, "COCO.idGeneral": self._gen,
                "COCO.idProceso": str(uuid.uuid4()),
                "COCO.idMensaje": str(uuid.uuid4())}
        st, _, raw = self._req(self.t3 + op, json.dumps(payload).encode(), hdrs,
                               "POST")
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            d = {}
        return st, d

    def _cookie(self, name):
        return next((c.value for c in self._jar if c.name == name), None)

    # -- steps ---------------------------------------------------------------
    def start(self, phone):
        """Begin the login and make O2 send the SMS to `phone`."""
        self.phone = normalize_phone(phone)
        self._req(self.base + "/")
        st, h, _ = self._req(self.base + "/sapi/oauth/pkce/authorize?platform=web"
                             "&deviceid=" + urllib.parse.quote(self.device_id))
        url = h.get("Location") if h else None
        for _ in range(5):                          # up to the login page
            if not url or "#" in url or "sessionData=" in url:
                break
            st, h, _ = self._req(url)
            url = h.get("Location") if h else None
        if not url:
            raise LoginError("O2 no inició el login (HTTP %s)" % st)
        frag = url.split("#", 1)[1] if "#" in url else urllib.parse.urlparse(url).query
        q = _qs(frag.split("?", 1)[1] if "?" in frag else frag)
        sid, sdata = q.get("sessionID", ""), q.get("sessionData", "")
        if not (sid and sdata):
            raise LoginError("no se obtuvo la sesión de login de Telefónica")
        st, d = self._t3("manageCredentialMobileO2",
                         {"mobile": self.phone, "sessionID": sid,
                          "sessionData": sdata, "consumerId": CONSUMER_ID})
        if st != 200 or not d.get("newSessionData"):
            raise LoginError("O2 no envió el SMS (HTTP %s%s). ¿Es una línea de O2?"
                             % (st, _err(d)))
        self._sid, self._sdata = d.get("newSessionID"), d["newSessionData"]

    def verify(self, code):
        """Check the SMS code and return the new session:
        {jsessionid, validationkey, device_id, base_url, msisdn}."""
        if not self._sdata:
            raise LoginError("primero hay que pedir el SMS")
        otp = re.sub(r"\D", "", code or "")
        if not otp:
            raise LoginError("falta el código del SMS")
        st, d = self._t3("verifyCredentialMobileO2",
                         {"otp": otp, "sessionData": self._sdata,
                          "sessionID": self._sid, "Mobile": self.phone})
        redirect = d.get("redirectUri", "") if isinstance(d, dict) else ""
        if st != 200 or not redirect.startswith(self.base + "/sapi/login/oauth"):
            raise LoginError("código no aceptado (HTTP %s%s)" % (st, _err(d)))
        self._req(redirect)
        js, vk = self._cookie("JSESSIONID"), self._cookie("validationKey")
        if not (js and vk):
            raise LoginError("O2 no creó la sesión tras validar el código")
        return {"jsessionid": js, "validationkey": vk,
                "device_id": self.device_id, "base_url": self.base,
                "msisdn": "+34" + self.phone}
