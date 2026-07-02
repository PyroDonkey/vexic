from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

from vexic.storage.connection import StorageTarget

if TYPE_CHECKING:
    from vexic.hosted import HostedTenant

PLATFORM_API_BASE = "https://api.turso.tech"

HttpCall = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, dict]]


def _require(env: Mapping[str, str], name: str) -> str:
    v = env.get(name, "").strip()
    if not v:
        raise ValueError(f"missing required env var: {name}")
    return v


def control_plane_target(env: Mapping[str, str]) -> StorageTarget:
    return StorageTarget(
        _require(env, "TURSO_DATABASE_URL"),
        auth_token=_require(env, "TURSO_AUTH_TOKEN"),
    )


@dataclass(frozen=True)
class ReconcileReport:
    """Result of reconciling the Turso Platform API's list of databases
    against the control-plane catalog's tenant -> customer_target mapping
    (COA-273 Task 13, P2->P3 split-brain window).

    - ``matched``: tenant_id -> target present both in the catalog and on
      the platform.
    - ``orphan_databases``: platform database identifiers not referenced by
      any tenant in the catalog.
    - ``dangling_targets``: tenant_id -> target referenced by a tenant's
      catalog row but not present among the platform databases.

    Tenants whose ``customer_target`` is ``None`` (local-only storage) are
    excluded from all three collections -- they are not part of the Turso
    split-brain question.
    """

    matched: Mapping[str, str]
    orphan_databases: frozenset[str]
    dangling_targets: Mapping[str, str]


def reconcile_tenant_databases(
    platform_db_targets: Iterable[str],
    catalog_targets: Mapping[str, str | None],
) -> ReconcileReport:
    """Pure reconcile of Turso platform databases against the catalog.

    ``platform_db_targets`` is the set of database identifiers/DSNs the
    caller already obtained from the Turso Platform API's list-databases
    call (no network I/O happens here). ``catalog_targets`` is the
    tenant_id -> customer_target mapping from the control-plane catalog
    (Task 11's column); a ``None`` target means the tenant is local-only
    and is ignored entirely (neither matched nor dangling).

    Comparison is a simple string equality between a platform target and a
    tenant's ``customer_target`` string -- both are expected to be the same
    DSN/identifier form the catalog stores. No normalization beyond that is
    attempted here; callers supplying differently-shaped identifiers (e.g.
    a bare database name vs. a full ``libsql://`` DSN) must normalize before
    calling.

    No network access, no secrets, no env reads -- deterministic given its
    two arguments.
    """
    platform_set = frozenset(platform_db_targets)

    matched: dict[str, str] = {}
    dangling_targets: dict[str, str] = {}
    referenced_targets: set[str] = set()

    for tenant_id, target in catalog_targets.items():
        if target is None:
            continue
        referenced_targets.add(target)
        if target in platform_set:
            matched[tenant_id] = target
        else:
            dangling_targets[tenant_id] = target

    orphan_databases = frozenset(
        db for db in platform_set if db not in referenced_targets
    )

    return ReconcileReport(
        matched=matched,
        orphan_databases=orphan_databases,
        dangling_targets=dangling_targets,
    )


def _default_http_call(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, dict]:
    """Stdlib ``urllib.request``-based transport (no new dependency).

    Returns ``(status, json_dict)``. Non-JSON or empty bodies decode to
    ``{}``. Non-2xx responses are NOT raised here -- ``urllib`` raises
    ``HTTPError`` on those, which this function catches and converts back
    into a normal ``(status, json_dict)`` tuple so callers handle status
    codes uniformly regardless of transport.
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed https host
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        raw = exc.read()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed


def _looks_like_conflict(status: int, payload: dict) -> bool:
    if status == 409:
        return True
    message = str(payload.get("error", "")).lower()
    return "already exists" in message


class TursoProvisioningPort:
    """Creates/mints/destroys per-tenant Turso databases via the Turso
    Platform API (COA-273 P4).

    Secrets (the platform API token) are read only in ``adapters/`` --
    ``src/vexic`` never sees them. The HTTP transport is injectable so unit
    tests never touch the network; ``_default_http_call`` (stdlib
    ``urllib.request``) is used when no transport is supplied.

    Never logs or prints the platform token or a minted jwt. Non-2xx error
    messages never include the token.
    """

    def __init__(
        self,
        org: str,
        group: str,
        *,
        http_call: HttpCall | None = None,
        platform_token: str,
    ) -> None:
        self.org = org
        self.group = group
        self._http_call: HttpCall = http_call if http_call is not None else _default_http_call
        self._platform_token = platform_token

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TursoProvisioningPort:
        return cls(
            _require(env, "TURSO_ORG"),
            _require(env, "TURSO_GROUP"),
            platform_token=_require(env, "TURSO_PLATFORM_API_TOKEN"),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._platform_token}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str, query: Mapping[str, str] | None = None) -> str:
        base = f"{PLATFORM_API_BASE}/v1/organizations/{self.org}{path}"
        if query:
            return f"{base}?{urlencode(query)}"
        return base

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: dict | None = None,
    ) -> tuple[int, dict]:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        return self._http_call(method, self._url(path, query), self._headers(), payload)

    def _dsn_from_database_body(self, payload: dict) -> str:
        hostname = payload["database"]["Hostname"]
        return f"libsql://{hostname}"

    def create_database(self, name: str) -> str:
        """Idempotent create: on conflict (409 or an error body containing
        "already exists"), resolves the existing database's DSN via GET
        instead of raising.
        """
        status, payload = self._call(
            "POST", "/databases", body={"name": name, "group": self.group}
        )
        if 200 <= status < 300:
            return self._dsn_from_database_body(payload)
        if _looks_like_conflict(status, payload):
            get_status, get_payload = self._call("GET", f"/databases/{name}")
            if 200 <= get_status < 300:
                return self._dsn_from_database_body(get_payload)
            raise RuntimeError(
                f"Turso create_database({name!r}) conflicted, then GET failed with "
                f"status {get_status}."
            )
        raise RuntimeError(
            f"Turso create_database({name!r}) failed with status {status}."
        )

    def mint_token(
        self, db_name: str, *, expiration: str = "5m", read_only: bool = True
    ) -> str:
        """Returns the raw jwt. Never logged."""
        authorization = "read-only" if read_only else "full-access"
        status, payload = self._call(
            "POST",
            f"/databases/{db_name}/auth/tokens",
            query={"expiration": expiration, "authorization": authorization},
        )
        if not (200 <= status < 300):
            raise RuntimeError(
                f"Turso mint_token({db_name!r}) failed with status {status}."
            )
        return payload["jwt"]

    def destroy_database(self, name: str) -> None:
        status, payload = self._call("DELETE", f"/databases/{name}")
        if not (200 <= status < 300):
            raise RuntimeError(
                f"Turso destroy_database({name!r}) failed with status {status}."
            )

    def provision(
        self, name: str, *, expiration: str = "5m", read_only: bool = True
    ) -> tuple[str, str]:
        """Composes create_database + mint_token. If mint_token fails after
        a successful create_database, performs a COMPENSATING
        destroy_database and re-raises -- no half-provisioned DB is left
        behind.
        """
        dsn = self.create_database(name)
        try:
            token = self.mint_token(name, expiration=expiration, read_only=read_only)
        except Exception:
            # Best-effort compensation. A failing destroy must not mask the
            # original mint_token error, so swallow the cleanup exception and
            # let the bare re-raise surface the root cause.
            try:
                self.destroy_database(name)
            except Exception:
                pass
            raise
        return dsn, token


class TenantTokenCache:
    """In-process TTL cache of short-lived per-tenant Turso tokens
    (COA-273 Task 15, P4).

    Mints a fresh, DB-scoped token via ``TursoProvisioningPort.mint_token``
    on cache miss/expiry and caches it in memory only, keyed by
    ``db_name``. Raw tokens are NEVER persisted anywhere -- not to the
    control-plane catalog, not to disk -- per ADR 0019 ("mint short-lived
    tokens, do not persist raw"). The cache is a plain ``dict`` attribute
    on the instance; process restart or GC drops it, which is intentional.

    ``ttl_seconds`` (cache lifetime) must be kept shorter than the minted
    token's own ``expiration`` (e.g. 600s TTL vs. a 15m mint expiry) so a
    cached token is always re-minted well before the underlying jwt itself
    would expire -- callers never hand out a token that is valid per the
    cache but rejected by Turso.

    The ``clock`` is injected (defaults to ``time.monotonic``) and is the
    *only* time source consulted in ``get_token`` -- this makes expiry
    deterministic and testable via a fake clock, with no wall-clock reads
    in the logic.
    """

    def __init__(
        self,
        port: TursoProvisioningPort,
        *,
        ttl_seconds: int = 600,
        clock: Callable[[], float] = time.monotonic,
        expiration: str = "15m",
        read_only: bool = False,
    ) -> None:
        self._port = port
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._expiration = expiration
        self._read_only = read_only
        self._cache: dict[str, tuple[str, float]] = {}

    def get_token(self, db_name: str) -> str:
        """Returns a cached token for ``db_name`` if present and not yet
        expired (per ``clock()`` and ``ttl_seconds``); otherwise mints a
        fresh one via the port, caches it with the current timestamp, and
        returns it. Never logs or prints the token.
        """
        cached = self._cache.get(db_name)
        if cached is not None:
            token, minted_at = cached
            if self._clock() - minted_at < self._ttl_seconds:
                return token
        token = self._port.mint_token(
            db_name, expiration=self._expiration, read_only=self._read_only
        )
        self._cache[db_name] = (token, self._clock())
        return token

    def invalidate(self, db_name: str) -> None:
        """Drops any cached token for ``db_name``, forcing a re-mint on
        the next ``get_token`` call. No-op if nothing is cached.
        """
        self._cache.pop(db_name, None)


def _db_name_from_dsn(customer_target: str, org: str) -> str:
    """Derive the Turso database NAME from a customer-target DSN.

    The DSN hostname is ``{db_name}-{org}.<region>.turso.io`` (Turso composes
    the public hostname by suffixing the org slug onto the database name). The
    db name is therefore the first hostname label with the ``-{org}`` suffix
    removed. ``removesuffix`` is a no-op when the label does not carry the
    suffix, so a bare label is used verbatim. This derivation lives in
    ``adapters/`` because it is the only layer that knows ``org``.
    """
    host = urlsplit(customer_target).hostname or ""
    label = host.split(".")[0]
    return label.removesuffix(f"-{org}")


def make_customer_target_resolver(
    token_cache: TenantTokenCache, *, org: str
) -> "Callable[[HostedTenant], StorageTarget | None]":
    """Build the per-tenant customer-memory resolver (COA-273 Task 16, P4).

    The returned resolver, given a ``HostedTenant``:
    - returns ``None`` when the tenant has no ``customer_target`` (local
      storage path, unchanged behavior);
    - otherwise derives the Turso db NAME from the stored DSN hostname (see
      ``_db_name_from_dsn``), mints/reuses a short-lived DB-scoped token via
      ``token_cache.get_token(db_name)``, and returns a connectable
      ``StorageTarget(customer_target, <jwt>)``.

    Tokens are minted here (in ``adapters/``, the only place secrets live) and
    never persisted -- the catalog stores only the DSN. The minted jwt is
    never logged.
    """

    def resolve(tenant: "HostedTenant") -> StorageTarget | None:
        customer_target = tenant.customer_target
        if not customer_target:
            return None
        db_name = _db_name_from_dsn(customer_target, org)
        return StorageTarget(customer_target, token_cache.get_token(db_name))

    return resolve
