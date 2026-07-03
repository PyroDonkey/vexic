from urllib.parse import urlsplit


def _is_loopback_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    return hostname == "localhost" or hostname == "::1" or hostname.startswith("127.")


def require_http_url(name: str, value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must use http or https.")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        # These URLs carry bearer API keys and memory queries; refuse to send
        # them in cleartext off-machine (mirrors the libSQL plaintext-token
        # refusal in storage.connection).
        raise ValueError(f"{name} must use https for non-loopback hosts.")
    return url
