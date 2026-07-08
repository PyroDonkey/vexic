import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vexic_runtime_does_not_import_predecessor_engine() -> None:
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


def test_core_carries_no_kms_or_crypto_provider_sdks() -> None:
    # ADR 0023: the ContentCodec port lives in core, but key material and
    # KMS/crypto SDK wiring belong to adapters/hosts (ADR 0008 boundary).
    forbidden = (
        "import boto3",
        "import botocore",
        "from cryptography",
        "import cryptography",
        "google.cloud.kms",
        "azure.keyvault",
    )
    offenders: list[str] = []
    for path in (ROOT / "src" / "vexic").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")

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


def test_core_hosted_http_does_not_own_hosted_write_adapter() -> None:
    text = (ROOT / "src" / "vexic" / "hosted_http.py").read_text(encoding="utf-8")

    for forbidden in (
        '@app.post("/v1/append_transcript")',
        '@app.post("/v1/ingest_source_transcript")',
        "class HostedAppendTranscriptBody",
        "class HostedIngestSourceTranscriptBody",
        "def _handle_hosted_write",
        "def _write_scope_from_headers",
        "AppendTranscriptRequest",
        "IngestSourceTranscriptRequest",
        "MAX_APPEND_MESSAGES",
        "service.api_keys.authenticate(api_key)",
    ):
        assert forbidden not in text


def test_console_and_website_are_not_tracked_in_this_repository() -> None:
    # console/ and website/ were extracted to a private repo. This checks
    # tracked files (not local directory presence) so untracked local
    # artifacts left over in a worktree don't produce a false failure.
    tracked = subprocess.run(
        ["git", "ls-files", "console", "website"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert tracked == ""


def test_public_tree_does_not_embed_tracking_references() -> None:
    """Code and non-ADR docs must not reference the private issue tracker.

    Allowed locations (per docs/ai/AGENTS.md): docs/adr/ (decision
    provenance) and docs/provenance.md. The match is case-insensitive so a
    lowercase real id (e.g. coa-281) cannot slip past the guard; generic
    placeholders like coa-<id> never match because \\d+ requires a digit.
    """
    ticket_pattern = re.compile(r"\b" + "C" + r"OA-\d+\b", re.IGNORECASE)
    allowed = ("docs/adr/", "docs/provenance.md")
    offenders: list[str] = []
    scan_dirs = ("src", "tests", "adapters", "scripts", "docs")
    for scan in scan_dirs:
        for path in (ROOT / scan).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md"}:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == "tests/test_public_boundary.py":
                continue
            if rel.startswith(allowed):
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for line_number, line in enumerate(lines, 1):
                if ticket_pattern.search(line):
                    offenders.append(f"{rel}:{line_number}: {line.strip()}")

    assert offenders == []
