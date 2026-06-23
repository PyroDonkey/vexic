from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vexic_runtime_does_not_import_coalescent_engine() -> None:
    source_files = (ROOT / "src" / "vexic").rglob("*.py")
    offenders: list[str] = []
    for path in source_files:
        text = path.read_text(encoding="utf-8")
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.startswith("from engine.") or line.startswith("import engine.")
        ]
        if lines:
            offenders.append(f"{path.relative_to(ROOT)}: {lines}")

    assert offenders == []


def test_hosted_core_does_not_own_infrastructure_provisioning() -> None:
    text = (ROOT / "src" / "vexic" / "hosted.py").read_text(encoding="utf-8")
    mcp_stdio_text = (ROOT / "src" / "vexic" / "mcp_stdio.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "class HostedTenantCatalog",
        "class HostedApiKeyStore",
        "class HostedApiKey:",
        "class ProvisionedApiKey",
        "import hashlib",
        "import hmac",
        "self.audit_events",
        "self.usage_events",
    ):
        assert forbidden not in text

    for forbidden in (
        "import urllib",
        "urlopen(",
        "HostedHttpMemoryServiceClient",
    ):
        assert forbidden not in mcp_stdio_text
