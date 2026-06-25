from __future__ import annotations

import importlib.util
import re
import tomllib
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = ROOT / "scripts" / "check_release_tag.py"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "publish-pypi.yml"
HOSTED_DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-hosted.yml"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_release_tag", CHECK_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pypi" / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_release_tag_checker_rejects_mismatched_dispatch_input(monkeypatch) -> None:
    checker = _load_checker()
    version = _project_version()
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("PROJECT_DIR", "pypi")
    monkeypatch.setenv("GITHUB_REF_NAME", f"v{version}")
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")

    assert checker.main() == 1


def test_release_tag_checker_accepts_matching_version_tag(monkeypatch) -> None:
    checker = _load_checker()
    version = _project_version()
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("PROJECT_DIR", "pypi")
    monkeypatch.setenv("GITHUB_REF_NAME", f"v{version}")
    monkeypatch.setenv("RELEASE_TAG", f"v{version}")

    assert checker.main() == 0


@pytest.mark.parametrize(
    "pyproject_text",
    [
        None,
        "project =",
        "[project]\nname = \"vexic\"\n",
    ],
)
def test_release_tag_checker_reports_project_version_load_errors(
    monkeypatch, tmp_path, capsys, pyproject_text: str | None
) -> None:
    checker = _load_checker()
    if pyproject_text is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))

    assert checker.main() == 1
    captured = capsys.readouterr()
    assert (
        f"::error::unable to read project version from {tmp_path / 'pyproject.toml'}:"
        in captured.err
    )


def test_publish_workflow_requires_matching_version_tag() -> None:
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    required = [
        "release_tag:",
        "required: true",
        "if: github.ref_type == 'tag' && startsWith(github.ref_name, 'v') && github.ref_name == inputs.release_tag",
        "working-directory: pypi",
        "RELEASE_TAG: ${{ inputs.release_tag }}",
        "GITHUB_REF_NAME: ${{ github.ref_name }}",
        "uv run python ../scripts/check_release_tag.py",
        "uv build --sdist --wheel --out-dir dist --clear pypi",
    ]

    assert [item for item in required if item not in workflow] == []


def test_pypi_placeholder_does_not_package_real_source() -> None:
    data = tomllib.loads((ROOT / "pypi" / "pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["name"] == "vexic"
    assert data["project"]["version"] == "0.0.0"
    assert data["tool"]["setuptools"]["packages"] == []


def test_hosted_deploy_workflow_tests_builds_and_deploys_railway() -> None:
    workflow = HOSTED_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    required = [
        "branches: [main]",
        "workflow_dispatch:",
        "concurrency:",
        "cancel-in-progress: false",
        "run: uv run pytest",
        "needs: test",
        "docker build --tag vexic-hosted:${{ github.sha }} .",
        "needs: docker-build",
        "RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}",
        "npm install -g @railway/cli@5.23.1",
        "railway up --ci --project ${{ vars.RAILWAY_PROJECT_ID }} --environment production --service vexic",
        "curl --fail --retry 12 --retry-all-errors --retry-delay 10 --connect-timeout 5 --max-time 15 --retry-connrefused https://api.vexic.dev/health",
    ]

    assert [item for item in required if item not in workflow] == []
    assert re.search(
        r"(?ms)^  test:\n.*?run: uv run pytest\n\n  docker-build:\n.*?needs: test\n"
        r".*?docker build --tag vexic-hosted:\$\{\{ github.sha \}\} \.\n\n"
        r"  deploy:\n.*?needs: docker-build\n.*?RAILWAY_TOKEN: \$\{\{ secrets.RAILWAY_TOKEN \}\}"
        r".*?npm install -g @railway/cli@5\.23\.1\n"
        r".*?railway up --ci --project \$\{\{ vars.RAILWAY_PROJECT_ID \}\} --environment production --service vexic\n"
        r".*?curl --fail --retry 12 --retry-all-errors --retry-delay 10 --connect-timeout 5 --max-time 15 --retry-connrefused https://api\.vexic\.dev/health",
        workflow,
    )
