import pytest

from vexic.url_policy import require_http_url


def test_require_http_url_accepts_http_and_https_case_insensitively() -> None:
    assert require_http_url("base_url", " HTTPS://api.example.test/ ") == "HTTPS://api.example.test"


@pytest.mark.parametrize("url", ["file:///tmp/vexic", "https:///missing-host", "api.example.test"])
def test_require_http_url_rejects_non_http_or_hostless_urls(url: str) -> None:
    with pytest.raises(ValueError, match="base_url.*http"):
        require_http_url("base_url", url)
