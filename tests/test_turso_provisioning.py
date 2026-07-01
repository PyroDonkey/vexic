from urllib.parse import parse_qs, urlparse

import pytest

from adapters.turso_adapter import TursoProvisioningPort


PLATFORM_TOKEN = "secret-platform-token-xyz"  # noqa: S105 - test fixture only


def _create_body(db_name: str, org: str) -> dict:
    return {
        "database": {
            "Hostname": f"{db_name}-{org}.aws-us-west-2.turso.io",
            "Name": db_name,
            "DbId": "db-id-123",
        },
        "password": "unused",
        "username": "unused",
    }


class FakeTransport:
    """Records calls and returns canned (status, json) responses keyed by
    (method, path-without-query). Tests configure `responses` up front and
    can inspect `.calls` afterward.
    """

    def __init__(self, responses: dict[tuple[str, str], tuple[int, dict]]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None):
        parsed = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self.calls.append((method, parsed.path, query))
        self.headers_seen = headers
        key = (method, parsed.path)
        if key not in self.responses:
            raise AssertionError(f"unexpected call: {method} {parsed.path}")
        return self.responses[key]


def test_create_database_returns_dsn_from_200_body():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases"): (200, _create_body(name, org)),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    dsn = port.create_database(name)

    assert dsn == f"libsql://{name}-{org}.aws-us-west-2.turso.io"
    method, path, _ = transport.calls[0]
    assert method == "POST"
    assert path == f"/v1/organizations/{org}/databases"
    assert transport.headers_seen["Authorization"] == f"Bearer {PLATFORM_TOKEN}"


def test_mint_token_returns_jwt_and_sets_query_params():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases/{name}/auth/tokens"): (
                200,
                {"jwt": "the-jwt-value"},
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    jwt = port.mint_token(name, expiration="5m", read_only=True)

    assert jwt == "the-jwt-value"
    method, path, query = transport.calls[0]
    assert method == "POST"
    assert path == f"/v1/organizations/{org}/databases/{name}/auth/tokens"
    assert query["expiration"] == "5m"
    assert query["authorization"] == "read-only"


def test_mint_token_full_access_when_not_read_only():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases/{name}/auth/tokens"): (
                200,
                {"jwt": "full-access-jwt"},
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    jwt = port.mint_token(name, expiration="1h", read_only=False)

    assert jwt == "full-access-jwt"
    _, _, query = transport.calls[0]
    assert query["authorization"] == "full-access"
    assert query["expiration"] == "1h"


def test_destroy_database_issues_delete_to_right_url():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("DELETE", f"/v1/organizations/{org}/databases/{name}"): (
                200,
                {"database": name},
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    port.destroy_database(name)

    method, path, _ = transport.calls[0]
    assert method == "DELETE"
    assert path == f"/v1/organizations/{org}/databases/{name}"


def test_create_database_idempotent_on_conflict_resolves_via_get():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases"): (
                409,
                {"error": "database already exists"},
            ),
            ("GET", f"/v1/organizations/{org}/databases/{name}"): (
                200,
                _create_body(name, org),
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    dsn = port.create_database(name)

    assert dsn == f"libsql://{name}-{org}.aws-us-west-2.turso.io"
    methods = [call[0] for call in transport.calls]
    assert methods == ["POST", "GET"]


def test_provision_compensates_with_destroy_when_mint_fails():
    org, group, name = "acme-org", "default", "tenant-a"

    class FailingMintTransport(FakeTransport):
        def __call__(self, method, url, headers, body):
            parsed = urlparse(url)
            if method == "POST" and parsed.path.endswith("/auth/tokens"):
                self.calls.append((method, parsed.path, {}))
                raise RuntimeError("mint token failed: 500 server error")
            return super().__call__(method, url, headers, body)

    transport = FailingMintTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases"): (200, _create_body(name, org)),
            ("DELETE", f"/v1/organizations/{org}/databases/{name}"): (
                200,
                {"database": name},
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    with pytest.raises(RuntimeError, match="mint token failed"):
        port.provision(name)

    methods = [call[0] for call in transport.calls]
    assert methods == ["POST", "POST", "DELETE"]


def test_create_database_non_2xx_raises_without_leaking_token():
    org, group, name = "acme-org", "default", "tenant-a"
    transport = FakeTransport(
        {
            ("POST", f"/v1/organizations/{org}/databases"): (
                500,
                {"error": "internal server error"},
            ),
        }
    )
    port = TursoProvisioningPort(org, group, http_call=transport, platform_token=PLATFORM_TOKEN)

    with pytest.raises(RuntimeError) as exc_info:
        port.create_database(name)

    assert PLATFORM_TOKEN not in str(exc_info.value)


def test_from_env_builds_port_from_expected_vars():
    env = {
        "TURSO_ORG": "acme-org",
        "TURSO_GROUP": "default",
        "TURSO_PLATFORM_API_TOKEN": PLATFORM_TOKEN,
    }
    port = TursoProvisioningPort.from_env(env)

    assert port.org == "acme-org"
    assert port.group == "default"


def test_from_env_missing_var_raises():
    with pytest.raises(ValueError):
        TursoProvisioningPort.from_env({"TURSO_ORG": "acme-org"})
