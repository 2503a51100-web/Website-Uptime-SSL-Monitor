"""SSL certificate monitoring (standard library only: ssl + socket).

check_ssl() connects to a server, validates its certificate the same way a
browser would, and reports the expiry date and the days remaining.

It has no Flask or database code, so it can be reused unchanged inside an
AWS Lambda function later.
"""

import socket
import ssl
from datetime import datetime, timezone

WARNING_DAYS = 30  # certificates expiring within this many days get a warning

# OpenSSL verification error code for "certificate has expired"
X509_V_ERR_CERT_HAS_EXPIRED = 10


def _result(status, message, expiry=None, days=None):
    return {
        "ssl_status": status,
        "ssl_expiry_date": expiry.strftime("%Y-%m-%d") if expiry else None,
        "ssl_days_remaining": days,
        "ssl_message": message,
    }


def not_applicable(message="The website does not use HTTPS, so there is no certificate to check."):
    return _result("Not Applicable", message)


def unavailable(message):
    return _result("Unavailable", message)


def _days_until(not_after):
    return (not_after - datetime.now(timezone.utc)).days


def _issuer_name(cert):
    """Pick a readable issuer name out of the dict returned by getpeercert()."""
    try:
        issuer = dict(item[0] for item in cert.get("issuer", ()))
        return issuer.get("organizationName") or issuer.get("commonName")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reading the expiry date of a certificate that FAILED validation.
# getpeercert() returns an empty dict for such certificates, so we ask for the
# raw DER bytes and read the validity period out of them ourselves.
# ---------------------------------------------------------------------------

def _read_tlv(data, pos):
    """Read one ASN.1 element. Returns (tag, value_start, value_end)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    return tag, pos, pos + length


def _parse_asn1_time(tag, raw):
    text = raw.decode("ascii")
    if tag == 0x17:  # UTCTime: YYMMDDHHMMSSZ
        year = int(text[:2])
        year += 1900 if year >= 50 else 2000
        text = f"{year}{text[2:]}"
    # GeneralizedTime: YYYYMMDDHHMMSSZ
    return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _expiry_from_der(der):
    """Return the notAfter date (UTC) from a DER encoded X.509 certificate."""
    _, pos, _ = _read_tlv(der, 0)          # Certificate ::= SEQUENCE
    _, pos, _ = _read_tlv(der, pos)        # tbsCertificate ::= SEQUENCE
    tag, start, end = _read_tlv(der, pos)
    if tag == 0xA0:                        # optional [0] version
        tag, start, end = _read_tlv(der, end)
    # (start, end) is now the serialNumber; skip signature algorithm and issuer
    for _ in range(2):
        _, start, end = _read_tlv(der, end)
    _, v_start, _ = _read_tlv(der, end)    # validity ::= SEQUENCE { notBefore, notAfter }
    tag, start, end = _read_tlv(der, v_start)       # notBefore
    tag, start, end = _read_tlv(der, end)           # notAfter
    return _parse_asn1_time(tag, der[start:end])


def _fetch_expiry_unverified(hostname, port, timeout):
    """Connect WITHOUT validating, only to read the expiry date. None on failure."""
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as tls:
                der = tls.getpeercert(binary_form=True)
        return _expiry_from_der(der)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def check_ssl(hostname, port=443, timeout=5.0):
    """Check the SSL certificate of hostname:port.

    Returns a dict with:
      ssl_status          Valid | Expiring Soon | Expired | Invalid | Unavailable
      ssl_expiry_date     "YYYY-MM-DD" (UTC) or None
      ssl_days_remaining  whole days left (negative if already expired) or None
      ssl_message         short human-readable explanation
    """
    context = ssl.create_default_context()  # validates chain + hostname like a browser
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            with context.wrap_socket(sock, server_hostname=hostname) as tls:
                cert = tls.getpeercert()

    except ssl.SSLCertVerificationError as exc:
        # Certificate was received but is not trustworthy (expired, self-signed,
        # wrong hostname, unknown issuer ...).
        reason = (getattr(exc, "verify_message", None) or str(exc)).strip().rstrip(".")
        reason = reason[:1].upper() + reason[1:]
        not_after = _fetch_expiry_unverified(hostname, port, timeout)
        days = _days_until(not_after) if not_after else None
        if getattr(exc, "verify_code", None) == X509_V_ERR_CERT_HAS_EXPIRED:
            return _result("Expired", "Certificate has expired.", not_after, days)
        return _result("Invalid", f"{reason}.", not_after, days)

    except ssl.SSLError as exc:
        return _result("Invalid", f"TLS handshake failed: {exc.reason or exc}.")

    except OSError as exc:  # timeout, DNS failure, connection refused ...
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return unavailable("Connection timed out while fetching the certificate.")
        return unavailable(f"Could not connect to fetch the certificate: {exc.strerror or exc}.")

    # Validation passed. Work out the expiry date.
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    days = _days_until(not_after)
    issuer = _issuer_name(cert)
    issued_by = f" Issued by {issuer}." if issuer else ""

    if days < 0:
        return _result("Expired", "Certificate has expired.", not_after, days)
    if days <= WARNING_DAYS:
        return _result(
            "Expiring Soon",
            f"Certificate expires in {days} day(s). Renew it soon.{issued_by}",
            not_after, days,
        )
    return _result("Valid", f"Certificate is valid and trusted.{issued_by}", not_after, days)
