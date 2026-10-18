"""HTTP downloads with timeouts and retries."""

import ssl
import time
import urllib.error
import urllib.request

import certifi
import truststore

USER_AGENT = "hilabs-scalene-tic-extractor/0.1 (+price transparency research)"
TIMEOUT_S = 60  # per socket operation, so large files are fine
ATTEMPTS = 3
BACKOFF_S = 2.0
CHUNK = 1 << 20

SSL_CONTEXTS = [truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), ssl.create_default_context(cafile=certifi.where())]
SSL_CONTEXTS[-1].load_default_certs()


class FetchError(Exception):
    pass


def _open(req):
    for context in SSL_CONTEXTS[:-1]:
        try:
            return urllib.request.urlopen(req, timeout=TIMEOUT_S, context=context)
        except urllib.error.URLError as err:
            if not isinstance(err.reason, ssl.SSLCertVerificationError):
                raise
    return urllib.request.urlopen(req, timeout=TIMEOUT_S, context=SSL_CONTEXTS[-1])


def fetch(url, dest=None):
    """Return the body of url, or stream it into the file dest if given.

    Network errors, HTTP 5xx/429 and incomplete downloads are retried with
    exponential backoff; other HTTP errors fail at once.
    """
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with _open(req) as resp:
                if dest is None:
                    return resp.read()
                size = 0
                with open(dest, "wb") as out:
                    while chunk := resp.read(CHUNK):
                        out.write(chunk)
                        size += len(chunk)
                expected = resp.headers.get("Content-Length")
                if expected is not None and int(expected) != size:
                    raise ConnectionError(f"incomplete download: {size} of {expected} bytes")
                return dest
        except urllib.error.HTTPError as err:
            if err.code < 500 and err.code != 429:
                raise FetchError(f"{url}: HTTP {err.code} {err.reason}") from err
            last = err
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = err
        if attempt < ATTEMPTS:
            time.sleep(BACKOFF_S * 2 ** (attempt - 1))
    raise FetchError(f"{url}: failed after {ATTEMPTS} attempts: {last!r}") from last
