"""
Extract a usable O2 Cloud web session from a HAR capture.

O2 authenticates with three cookies -- JSESSIONID, validationKey and PLC -- plus
the validationkey echoed as a query param. This pulls those out of the HAR.

It works by scanning the raw text for Cookie headers (no strict JSON parsing),
so it tolerates large or slightly truncated HAR exports. It prefers the most
recent *authenticated* request (one whose Cookie header carries validationKey).

Note: a HAR of a login contains LIVE session secrets -- keep it private.
"""

import re
import urllib.parse

_COOKIE_RE = re.compile(
    r'"name"\s*:\s*"[Cc]ookie"\s*,\s*"value"\s*:\s*"([^"\\]+)"')
_HOST_RE = re.compile(r'https://([a-zA-Z0-9.-]*o2online\.es)')


def _cookie_val(cookie_header, name):
    m = re.search(r'(?:^|;\s*)' + re.escape(name) + r'=([^;]+)', cookie_header)
    return m.group(1).strip() if m else None


def parse_har(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return parse_har_data(fh.read())


def parse_har_data(text):
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", "replace")
    found = {"jsessionid": "", "validationkey": "", "plc": "",
             "base_url": "https://cloud.o2online.es", "msisdn": ""}

    m = _HOST_RE.search(text)
    if m:
        found["base_url"] = "https://" + m.group(1)

    cookie_headers = _COOKIE_RE.findall(text)
    authed = [c for c in cookie_headers if "validationKey=" in c]
    src = authed[-1] if authed else (cookie_headers[-1] if cookie_headers else "")
    if src:
        found["jsessionid"] = _cookie_val(src, "JSESSIONID") or ""
        found["validationkey"] = _cookie_val(src, "validationKey") or ""
        found["plc"] = _cookie_val(src, "PLC") or ""

    if not found["validationkey"]:
        mv = re.search(r'validationkey=([0-9a-fA-F]{8,})', text)
        if mv:
            found["validationkey"] = mv.group(1)
    mm = re.search(r'[?&]msisdn=([^"&]+)', text)
    if mm:
        found["msisdn"] = urllib.parse.unquote(mm.group(1))
    return found
