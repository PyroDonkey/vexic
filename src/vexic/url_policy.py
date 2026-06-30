from urllib.parse import urlsplit


def require_http_url(name: str, value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must use http or https.")
    return url
