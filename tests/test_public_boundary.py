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


def test_core_hosted_http_does_not_own_control_plane_adapter() -> None:
    text = (ROOT / "src" / "vexic" / "hosted_http.py").read_text(encoding="utf-8")

    for forbidden in (
        "/control/v1",
        "_control_plane_storage_boundary",
        "VEXIC_CONTROL_PLANE_TOKENS",
    ):
        assert forbidden not in text


def test_console_boundary_is_documented_as_outside_vexic_package() -> None:
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    console_readme = (ROOT / "console" / "README.md").read_text(encoding="utf-8")
    console_layout = (
        ROOT / "console" / "app" / "console" / "layout.tsx"
    ).read_text(encoding="utf-8")

    for text in (" ".join(root_readme.split()), " ".join(console_readme.split())):
        assert "repo-local Next.js control-plane app" in text
        assert "not Vexic package runtime" in text
        assert "`vexic.*` entrypoint" in text

    assert "not memory-core runtime under src/vexic" in console_layout
