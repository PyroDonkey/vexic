from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_clerk_config_requires_client_and_server_keys() -> None:
    text = (ROOT / "console" / "lib" / "clerk-config.ts").read_text(encoding="utf-8")

    assert "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY" in text
    assert "CLERK_SECRET_KEY" in text
    assert "&&" in text


def test_clerk_proxy_includes_auto_proxy_matcher() -> None:
    text = (ROOT / "console" / "proxy.ts").read_text(encoding="utf-8")

    assert '"/api/control-plane/:path*"' in text
    assert '"/__clerk/:path*"' in text
