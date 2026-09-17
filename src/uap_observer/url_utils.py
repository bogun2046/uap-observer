"""URL normalization used for deterministic news deduplication."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
}


def normalize_url(url: str, *, base_url: str | None = None) -> str:
    absolute = urljoin(base_url, url) if base_url else url
    parts = urlsplit(absolute.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"Unsupported article URL: {url!r}")

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")

    query_items = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS:
            continue
        query_items.append((key, value))
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))
