"""HTTP downloads with timeouts and retries."""

import time
import urllib.error
import urllib.request

USER_AGENT = "hilabs-scalene-tic-extractor/0.1 (+price transparency research)"
TIMEOUT_S = 60  # per socket operation, so large files are fine
ATTEMPTS = 3
BACKOFF_S = 2.0
CHUNK = 1 << 20


class FetchError(Exception):
    pass


def fetch(url, dest=None):
    """Return the body of url, or stream it into the file dest if given.

    Network errors, HTTP 5xx/429 and incomplete downloads are retried with
    exponential backoff; other HTTP errors fail at once.
    """
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
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
