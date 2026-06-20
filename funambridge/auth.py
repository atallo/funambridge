"""
AWS Signature V4 verification for the S3 front-end.

We don't store AWS-style credentials elsewhere: the account's access_key/secret_key
ARE the S3 credentials, and a client must prove it knows the secret_key by signing
the request. This verifies header-based SigV4 (what aws-cli, rclone, WinSCP, boto,
... use). Behind an HTTPS reverse proxy the signed Host may differ from the one we
receive, so the caller can supply several host candidates to try.
"""

import hashlib
import hmac
import re
import urllib.parse

_CRED_RE = re.compile(r"Credential=([^/,\s]+)/([^/]+)/([^/]+)/([^/]+)/aws4_request")
_SH_RE = re.compile(r"SignedHeaders=([^,\s]+)")
_SIG_RE = re.compile(r"Signature=([0-9a-fA-F]+)")


def extract_access_key(headers, query_string):
    auth = headers.get("Authorization", "") or ""
    m = re.search(r"Credential=([^/,\s]+)", auth)
    if m:
        return m.group(1)
    qs = urllib.parse.parse_qs(query_string or "")
    if "X-Amz-Credential" in qs:
        return qs["X-Amz-Credential"][0].split("/")[0]
    if "AWSAccessKeyId" in qs:
        return qs["AWSAccessKeyId"][0]
    return None


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    k = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def _canonical_query(query_string):
    # sort by key then value; re-encode per RFC3986 (S3/SigV4 style)
    pairs = urllib.parse.parse_qsl(query_string or "", keep_blank_values=True)
    enc = sorted((urllib.parse.quote(k, safe="-_.~"),
                  urllib.parse.quote(v, safe="-_.~")) for k, v in pairs)
    return "&".join(f"{k}={v}" for k, v in enc)


def is_sigv4(headers):
    return (headers.get("Authorization", "") or "").startswith("AWS4-HMAC-SHA256")


def verify(method, raw_path, headers, secret_key, host_candidates):
    """Return True if the header SigV4 signature matches secret_key for any of
    the given host candidates. Only AWS4-HMAC-SHA256 header auth is verified."""
    auth = headers.get("Authorization", "") or ""
    mc = _CRED_RE.search(auth)
    ms = _SH_RE.search(auth)
    mg = _SIG_RE.search(auth)
    if not (mc and ms and mg):
        return False
    _ak, datestamp, region, service = mc.groups()
    signed_headers = ms.group(1).split(";")
    provided_sig = mg.group(1).lower()
    amzdate = headers.get("x-amz-date") or headers.get("X-Amz-Date") or ""
    path, _, query = raw_path.partition("?")
    payload_hash = (headers.get("x-amz-content-sha256")
                    or headers.get("X-Amz-Content-Sha256") or "UNSIGNED-PAYLOAD")

    # header lookup is case-insensitive
    lower = {k.lower(): v for k, v in headers.items()}
    canon_query = _canonical_query(query)
    scope = f"{datestamp}/{region}/{service}/aws4_request"

    for host in host_candidates:
        if not host:
            continue
        vals = dict(lower)
        vals["host"] = host
        try:
            canon_headers = "".join(
                f"{h}:{str(vals.get(h, '')).strip()}\n" for h in signed_headers)
        except Exception:  # noqa: BLE001
            continue
        canonical_request = "\n".join([
            method, path, canon_query, canon_headers,
            ";".join(signed_headers), payload_hash])
        sts = "\n".join([
            "AWS4-HMAC-SHA256", amzdate, scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        key = _signing_key(secret_key, datestamp, region, service)
        calc = hmac.new(key, sts.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc, provided_sig):
            return True
    return False
