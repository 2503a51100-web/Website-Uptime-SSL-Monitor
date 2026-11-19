"""Website availability monitoring.

check_website(url) does the whole job for one website: validate the URL,
check availability and response time with Requests, and check the SSL
certificate. It returns a plain dict.

This file does not import Flask or SQLite, so check_website() can be called
from an AWS Lambda handler later without any changes.
"""

import ipaddress
import os
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

import ssl_checker

# Timeouts in seconds. Can be changed with environment variables.
CONNECT_TIMEOUT = float(os.environ.get("MONITOR_CONNECT_TIMEOUT", "5"))
READ_TIMEOUT = float(os.environ.get("MONITOR_READ_TIMEOUT", "10"))

# Set BLOCK_PRIVATE_TARGETS=1 when the app is deployed on a public server so
# nobody can use it to probe localhost or internal networks. It stays off for
# local development so you can test against your own machine.
BLOCK_PRIVATE_TARGETS = os.environ.get("BLOCK_PRIVATE_TARGETS", "0") == "1"

USER_AGENT = "WebsiteUptimeSSLMonitor/1.0 (student project)"

# A website is UP when the server answers with a status code below 400
# (2xx success or 3xx redirect that ended well). 4xx and 5xx mean DOWN.
UP_BELOW_STATUS = 400

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$", re.IGNORECASE)


class InvalidURLError(ValueError):
    """Raised when the text entered by the user is not a usable website URL."""


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _is_valid_host(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    if host.lower() == "localhost":
        return True
    try:
        host = host.encode("idna").decode("ascii")  # allows international domains
    except UnicodeError:
        return False
    # a real domain name needs at least one dot, e.g. "example.com"
    return len(host) <= 253 and "." in host and bool(_HOSTNAME_RE.match(host))


def normalize_url(raw):
    """Clean up user input and return a full URL, or raise InvalidURLError.

    "google.com" becomes "https://google.com".
    """
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidURLError("Please enter a website URL.")
    url = raw.strip()
    if len(url) > 2048:
        raise InvalidURLError("The URL is too long.")
    if re.search(r"\s", url):
        raise InvalidURLError("The URL must not contain spaces.")
    if "://" not in url:
        url = "https://" + url

    try:
        parts = urlparse(url)
        parts.port  # raises ValueError for a bad port
    except ValueError:
        raise InvalidURLError("The URL is not valid.")

    if parts.scheme.lower() not in ("http", "https"):
        raise InvalidURLError("Only http:// and https:// URLs are supported.")
    if parts.username or parts.password:
        raise InvalidURLError("URLs containing a username or password are not supported.")
    if not parts.hostname or not _is_valid_host(parts.hostname):
        raise InvalidURLError("That does not look like a valid website address. Example: google.com")
    return url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_private_address(ip_text):
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _describe_connection_error(exc):
    """Turn a low-level connection error into a short readable message."""
    text = str(exc).lower()
    if "refused" in text or "10061" in text:
        return "Connection refused: the server is not accepting connections on that port."
    if "reset" in text or "10054" in text:
        return "Connection was reset by the server."
    if "name or service not known" in text or "getaddrinfo" in text or "nameresolution" in text \
            or "failed to resolve" in text or "no address associated" in text:
        return "DNS lookup failed: the domain name could not be resolved."
    if "unreachable" in text:
        return "Network unreachable: no route to the website."
    return "Could not connect to the website."


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def check_website(raw_url):
    """Check one website and return the result as a dict.

    Raises InvalidURLError if the URL itself is not valid. Every other problem
    (DNS failure, refused connection, timeout, SSL error, HTTP error codes)
    is a normal monitoring result: status "DOWN" with an error_message.

    Keys: url, status, status_code, response_time_ms, ssl_status,
          ssl_expiry_date, ssl_days_remaining, ssl_message, error_message,
          checked_at
    """
    url = normalize_url(raw_url)
    parts = urlparse(url)
    host = parts.hostname
    is_https = parts.scheme.lower() == "https"
    port = parts.port or (443 if is_https else 80)

    result = {
        "url": url,
        "status": "DOWN",
        "status_code": None,
        "response_time_ms": None,
        "error_message": None,
        "checked_at": _now_iso(),
    }
    result.update(
        ssl_checker.unavailable("Certificate not checked because the website could not be reached.")
        if is_https else ssl_checker.not_applicable()
    )

    # Step 1: DNS lookup (socket). Gives a clear error for unknown domains.
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        result["error_message"] = "DNS lookup failed: the domain name could not be resolved."
        return result
    except (UnicodeError, OSError):
        result["error_message"] = "The website address could not be resolved."
        return result

    if BLOCK_PRIVATE_TARGETS and any(_is_private_address(a) for a in addresses):
        result["error_message"] = "Checking private or local network addresses is not allowed on this server."
        return result

    # Step 2: HTTP request. stream=True stops Requests from downloading the
    # whole page, so the timing is: DNS + connect + TLS + request + first response bytes.
    response_received = False
    ssl_failed_in_http = False
    ssl_host, ssl_port, ssl_wanted = host, port, is_https

    started = time.perf_counter()
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True,
            stream=True,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        response_received = True
        result["status_code"] = response.status_code
        result["response_time_ms"] = round(elapsed_ms, 1)
        if response.status_code < UP_BELOW_STATUS:
            result["status"] = "UP"
        else:
            result["error_message"] = f"The server responded with HTTP {response.status_code} {response.reason or ''}".strip() + "."

        # If redirects moved us to another host/scheme, check that certificate.
        final = urlparse(response.url)
        ssl_wanted = final.scheme.lower() == "https"
        if ssl_wanted:
            ssl_host = final.hostname or host
            ssl_port = final.port or 443
        response.close()

    except requests.exceptions.SSLError:
        ssl_failed_in_http = True
        result["error_message"] = "SSL certificate problem: the secure connection could not be established."
    except requests.exceptions.ProxyError:
        result["error_message"] = "Could not connect through the configured proxy."
    except requests.exceptions.ConnectTimeout:
        result["error_message"] = f"Connection timed out after {CONNECT_TIMEOUT:g} seconds."
    except requests.exceptions.ReadTimeout:
        result["error_message"] = f"The server accepted the connection but did not respond within {READ_TIMEOUT:g} seconds."
    except requests.exceptions.ConnectionError as exc:
        result["error_message"] = _describe_connection_error(exc)
    except requests.exceptions.TooManyRedirects:
        result["error_message"] = "Too many redirects."
    except requests.exceptions.RequestException as exc:
        result["error_message"] = f"Request failed: {exc.__class__.__name__}."

    # Step 3: SSL certificate (only if the site answered, or failed on SSL).
    if response_received or ssl_failed_in_http:
        if ssl_wanted:
            result.update(ssl_checker.check_ssl(ssl_host, ssl_port))
            if ssl_failed_in_http and result["ssl_message"]:
                result["error_message"] = f"SSL certificate problem: {result['ssl_message']}"
        else:
            result.update(ssl_checker.not_applicable())

    return result
