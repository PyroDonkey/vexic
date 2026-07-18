from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from vexic import CONTRACT_VERSION
from vexic.error_reporting import validation_error_message
from vexic.contract import (
    DreamPhase,
    ExpandHistoryResult,
    ExpandHistoryRequest,
    FreshContextRequest,
    LoadActiveContextRequest,
    MemoryCapability,
    MemoryRequest,
    MemoryResult,
    MemoryScope,
    RedactionContext,
    SearchLongTermRequest,
    SearchTranscriptRequest,
    TriggerDreamPhaseRequest,
    TrustBoundary,
)
from vexic.hosted import (
    HostedAuthContext,
    HostedInMemoryRateLimiter,
    HostedMemoryService,
    HostedRateLimitExceeded,
    add_run_dream_phase_subcommand,
    dream_phase_ports_from_env,
    register_hosted_write_routes,
    resolve_control_plane_target,
    resolve_storage_backend,
    run_dream_phase_command,
)
from vexic.mcp_http import register_mcp_routes
from vexic.mcp_http import _scope_from_headers as _read_scope_from_headers
from vexic.ports import HostPortNotConfigured
from vexic.hosted_local import (
    HostedApiKeyStore,
    HostedTenantCatalog,
    hosted_auth_request_context,
)
from vexic.storage.errors import (
    is_operational_error,
    is_retryable_operational_error,
    is_unique_violation,
)

logger = logging.getLogger(__name__)


class _TursoProvisioning:
    """Injection seam for the ``turso`` factory branch.

    Wraps the two secret-bearing pieces that ``src/vexic`` is NOT permitted to
    construct directly (ADR 0019): the ``TursoProvisioningPort`` (platform API
    token) and the per-tenant customer-target resolver (mints DB-scoped jwts).
    Both are built in ``adapters.turso_adapter`` and imported lazily so the
    default import graph carries no Turso secrets, and tests can inject a fake
    with no real credentials.
    """

    def build_port(self, env: dict[str, str]) -> object:
        from adapters.turso_adapter import TursoProvisioningPort

        return TursoProvisioningPort.from_env(env)

    def build_resolver(
        self, token_cache: object, *, org: str, env: Mapping[str, str] | None = None
    ) -> object:
        from adapters.turso_adapter import (
            make_customer_target_resolver,
            query_deadline_from_env,
        )

        return make_customer_target_resolver(
            token_cache,
            org=org,
            query_deadline_seconds=query_deadline_from_env(env or {}),
        )

    def build_token_cache(self, port: object) -> object:
        from adapters.turso_adapter import TenantTokenCache

        return TenantTokenCache(port)

    def build_control_plane_target(self, env: dict[str, str]) -> object:
        from adapters.turso_adapter import control_plane_target

        return control_plane_target(env)


MAX_BODY_BYTES = 1_000_000
MAX_QUERY_CHARS = 1_000
MAX_LIMIT = 20
MAX_EXPAND_HISTORY_MESSAGES = 100
MAX_EXPAND_HISTORY_CHARS = 20_000
MIN_FRESH_CONTEXT_TOKEN_BUDGET = 1
MAX_FRESH_CONTEXT_TOKEN_BUDGET = 24_000

_RequestT = TypeVar("_RequestT", bound=MemoryRequest)
_ResultT = TypeVar("_ResultT", bound=MemoryResult)
_SearchRequestT = TypeVar("_SearchRequestT", SearchTranscriptRequest, SearchLongTermRequest)


class _HeaderBoundSearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = 5
    as_of: str | None = None
    event_after: str | None = None
    event_before: str | None = None


class _HeaderBoundFreshContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: int = 6_000
    redaction: RedactionContext = RedactionContext(forbidden_values=())


class _HeaderBoundLoadActiveContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token_budget: int = 24_000
    timezone_name: str = "UTC"
    redaction: RedactionContext = RedactionContext(forbidden_values=())


class _HeaderBoundExpandHistoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_message_id: int
    last_message_id: int
    redaction: RedactionContext = RedactionContext(forbidden_values=())


def create_app(
    service: HostedMemoryService | None = None,
    *,
    mcp_forbidden_secret_values: tuple[str, ...] = (),
    sweeper: object | None = None,
) -> FastAPI:
    service = service or create_service_from_env()
    lifespan = _sweeper_lifespan(sweeper, service) if sweeper is not None else None
    app = FastAPI(
        title="Vexic Hosted Memory",
        version=CONTRACT_VERSION,
        lifespan=lifespan,
    )
    register_mcp_routes(
        app,
        service,
        forbidden_secret_values=mcp_forbidden_secret_values,
    )

    @app.middleware("http")
    async def cap_body(
        request: Request,
        call_next: Callable[[Request], Awaitable[object]],
    ) -> object:
        with hosted_auth_request_context():
            if request.method in {"POST", "PUT", "PATCH"}:
                content_length = request.headers.get("content-length")
                if content_length is not None:
                    try:
                        body_size = int(content_length)
                    except ValueError:
                        return _error_response(
                            400,
                            "invalid_request",
                            "Invalid Content-Length header.",
                        )
                    if body_size < 0:
                        return _error_response(
                            400,
                            "invalid_request",
                            "Invalid Content-Length header.",
                        )
                    if body_size > MAX_BODY_BYTES:
                        return _error_response(
                            413,
                            "request_too_large",
                            "Request body is too large.",
                        )
                body = await request.body()
                if len(body) > MAX_BODY_BYTES:
                    return _error_response(413, "request_too_large", "Request body is too large.")
            return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error_response(422, "invalid_request", "Request body does not match the Vexic contract.")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "contract_version": CONTRACT_VERSION}

    register_hosted_write_routes(
        app,
        service,
        api_key_from_request=_api_key,
        handle_payload=_handle_payload,
        error_response=_error_response,
    )

    @app.post("/v1/search_transcript")
    async def search_transcript(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_search(
            request,
            payload,
            service,
            SearchTranscriptRequest,
            service.search_transcript,
        )

    @app.post("/v1/search_long_term")
    async def search_long_term(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_search(
            request,
            payload,
            service,
            SearchLongTermRequest,
            service.search_long_term,
        )

    @app.post("/v1/expand_history")
    async def expand_history(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_expand_history(request, payload, service)

    @app.post("/v1/fresh_context")
    async def fresh_context(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_fresh_context(request, payload, service)

    @app.post("/v1/load_active_context")
    async def load_active_context(
        request: Request, payload: dict[str, Any]
    ) -> JSONResponse:
        return await _handle_load_active_context(request, payload, service)

    @app.post("/v1/trigger_dream_phase")
    async def trigger_dream_phase(request: Request, payload: dict[str, Any]) -> JSONResponse:
        return await _handle_trigger_dream_phase(request, payload, service)

    @app.post("/v1/setup/exchange")
    async def setup_exchange(payload: dict[str, Any]) -> JSONResponse:
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            return _error_response(400, "invalid_request", "token is required.")
        try:
            exchange = service.api_keys.exchange_setup_token(token.strip())
        except PermissionError:
            return _error_response(401, "unauthorized", "Invalid setup token.")
        except Exception as exc:
            storage_error = _storage_error_response(exc)
            if storage_error is not None:
                return storage_error
            return _error_response(500, "internal_error", "Setup token exchange failed.")
        agent_scope = exchange.agent_scope
        return JSONResponse(
            {
                "apiKey": exchange.provisioned.raw_key,
                "keyId": exchange.provisioned.key_id,
                "projectId": exchange.project_id,
                "sessionId": exchange.session_id,
                "agentId": None if agent_scope == "shared" else agent_scope,
            }
        )

    return app


def create_service_from_env(
    *,
    turso_provisioning: _TursoProvisioning | None = None,
) -> HostedMemoryService:
    """Build the hosted memory service, per-store, from ``VEXIC_STORAGE_BACKEND``.

    Default (``local``, unset) preserves the exact prior behavior: a
    filesystem-rooted ``HostedTenantCatalog``/``HostedApiKeyStore`` under
    ``VEXIC_HOSTED_ROOT``, with no customer-target resolver (each tenant's
    local ``db_path`` is used unchanged).

    ``VEXIC_STORAGE_BACKEND=turso`` keeps the
    control-plane (``HostedTenantCatalog``/``HostedApiKeyStore``, i.e. auth +
    tenant lookup) LOCAL/filesystem-rooted exactly as in the ``local`` branch,
    but routes customer memory to a per-tenant Turso database. It builds a
    ``TursoProvisioningPort`` + ``TenantTokenCache`` (via the injected
    ``turso_provisioning`` seam, defaulting to ``adapters.turso_adapter`` --
    the only place secrets are read) and injects a per-tenant
    ``customer_target_resolver`` that mints short-lived, DB-scoped tokens on
    demand. If a dogfood tenant is named via ``VEXIC_DOGFOOD_TENANT_ID`` and it
    has no ``customer_target`` yet, the factory provisions a real per-tenant
    Turso DB for it (idempotent ``create_database`` + ``provision_tenant``) and
    stores only the DSN in the catalog -- NOT a single shared DB. The seam
    exists so tests can inject a fake with no real credentials.

    Dream-phase model ports are wired from ``VEXIC_DREAM_PHASE_ADAPTER`` /
    ``VEXIC_DREAM_PHASE_MODEL_GROUP`` via ``dream_phase_ports_from_env`` on
    both backends; unset leaves ports ``None`` (model-backed operations,
    including the ``search_long_term`` vector path, fail closed with
    ``HostPortNotConfigured``).
    """
    root = Path(os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))
    provisioning = turso_provisioning or _TursoProvisioning()
    catalog, keys, customer_target_resolver = _build_hosted_stores(
        root, provisioning=provisioning, env=os.environ
    )
    if resolve_storage_backend(os.environ) == "turso":
        if os.environ.get("VEXIC_PROVISION_EXISTING_TURSO_TARGETS", "").strip() == "1":
            catalog.provision_missing_customer_targets()
        _ensure_dogfood_tenant_target(catalog)
    return HostedMemoryService(
        catalog,
        keys,
        telemetry=catalog,
        rate_limiter=HostedInMemoryRateLimiter(),
        dream_phase_ports=dream_phase_ports_from_env(os.environ),
        customer_target_resolver=customer_target_resolver,
    )


def _build_hosted_stores(
    root: Path,
    *,
    provisioning: _TursoProvisioning,
    env: Mapping[str, str],
    customer_memory: bool = True,
) -> tuple[HostedTenantCatalog, HostedApiKeyStore, object | None]:
    """Build the catalog, API-key store, and customer-target resolver from the
    `VEXIC_CONTROL_PLANE_TARGET` / `VEXIC_STORAGE_BACKEND` flags.

    This is the single seam both `create_service_from_env` and the operator
    CLI (`main`) go through, so runbook commands and the serving app always
    resolve the same control plane from the same environment.

    ``customer_memory=False`` skips the `VEXIC_STORAGE_BACKEND` branch
    entirely: control-plane-only commands (`revoke-key`) must never be blocked
    by missing customer-provisioning configuration (`TURSO_ORG`, platform API
    token), or a production revocation could fail before the key is revoked.
    """
    backend = resolve_storage_backend(env) if customer_memory else "local"
    # Control-plane target is independent of the customer-memory backend
    # (ADR 0019 Addendum 4): `turso` routes the catalog + API-key
    # store to the managed libSQL control-plane database; `local` (default)
    # keeps the filesystem `control-plane.db`. `None` here means local.
    control_target = None
    if resolve_control_plane_target(env) == "turso":
        control_target = provisioning.build_control_plane_target(dict(env))
    customer_target_resolver = None
    if backend == "turso":
        org = env["TURSO_ORG"].strip()
        port = provisioning.build_port(dict(env))
        catalog = HostedTenantCatalog(
            root,
            control_target=control_target,
            customer_target_factory=lambda tenant_id: port.create_database(
                _customer_database_name(tenant_id)
            ),
        )
        keys = HostedApiKeyStore(root, control_target=control_target)
        cache = provisioning.build_token_cache(port)
        customer_target_resolver = provisioning.build_resolver(cache, org=org, env=env)
    else:
        catalog = HostedTenantCatalog(root, control_target=control_target)
        keys = HostedApiKeyStore(root, control_target=control_target)
    return catalog, keys, customer_target_resolver


def _ensure_dogfood_tenant_target(catalog: HostedTenantCatalog) -> None:
    """Provision a real per-tenant Turso DB for the dogfood tenant if it has
    no ``customer_target`` yet.

    The dogfood tenant is named by ``VEXIC_DOGFOOD_TENANT_ID`` (unset -> no-op,
    so the ``turso`` backend is usable with tenants provisioned out of band,
    e.g. the live test which builds the service directly with a tenant that
    already carries a DSN). When set and the tenant's ``customer_target`` is
    empty, re-provisioning lets the catalog's ``customer_target_factory``
    create a deterministic Turso-safe per-tenant database (idempotent) and
    store only the returned DSN -- never a shared DB, never a token.
    """
    tenant_id = os.environ.get("VEXIC_DOGFOOD_TENANT_ID", "").strip()
    if not tenant_id:
        return
    tenant = catalog.get_tenant(tenant_id)
    if tenant.customer_target:
        return
    catalog.provision_tenant(tenant_id, project_ids=tenant.project_ids)


def _customer_database_name(tenant_id: str) -> str:
    slug = "".join(ch if ch.isalnum() else "-" for ch in tenant_id.lower())
    slug = "-".join(part for part in slug.split("-") if part) or "tenant"
    digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:10]
    max_slug_chars = 48 - len("vexic--") - len(digest)
    safe_slug = slug[:max_slug_chars].rstrip("-") or "tenant"
    return f"vexic-{safe_slug}-{digest}"


async def _handle(
    request: Request,
    payload: _RequestT,
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    return await _handle_payload(api_key, payload, call)


async def _handle_search(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
    request_type: type[_SearchRequestT],
    call: Callable[[str, _SearchRequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        if "scope" in body:
            payload = request_type.model_validate(body)
        else:
            auth = _authenticate_for_header_scope(service, api_key)
            search = _HeaderBoundSearchBody.model_validate(body)
            for field_name in ("as_of", "event_after", "event_before"):
                if (
                    getattr(search, field_name) is not None
                    and field_name not in request_type.model_fields
                ):
                    return _error_response(
                        422,
                        "invalid_request",
                        "Request body does not match the Vexic contract.",
                    )
            search_kwargs: dict[str, Any] = {
                "scope": _read_scope_from_headers(request, auth),
                "query": search.query,
                "limit": search.limit,
            }
            for field_name in ("as_of", "event_after", "event_before"):
                if field_name in request_type.model_fields:
                    search_kwargs[field_name] = getattr(search, field_name)
            payload = request_type(**search_kwargs)
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return await _handle_payload(api_key, payload, call)


async def _handle_fresh_context(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        if "scope" in body:
            payload = FreshContextRequest.model_validate(body)
        else:
            auth = _authenticate_for_header_scope(service, api_key)
            fresh = _HeaderBoundFreshContextBody.model_validate(body)
            payload = FreshContextRequest(
                scope=_session_scope_from_headers(request, auth),
                token_budget=fresh.token_budget,
                redaction=fresh.redaction,
            )
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return await _handle_payload(api_key, payload, service.fresh_context)


async def _handle_expand_history(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        if "scope" in body:
            payload = ExpandHistoryRequest.model_validate(body)
        else:
            auth = _authenticate_for_header_scope(service, api_key)
            parsed = _HeaderBoundExpandHistoryBody.model_validate(body)
            payload = ExpandHistoryRequest(
                scope=_session_scope_from_headers(
                    request,
                    auth,
                    capability=MemoryCapability.EXPAND_HISTORY,
                ),
                first_message_id=parsed.first_message_id,
                last_message_id=parsed.last_message_id,
                redaction=parsed.redaction,
            )
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return await _handle_payload(
        api_key,
        payload,
        lambda key, body_payload: service.expand_history(
            key,
            body_payload,
            max_rows=MAX_EXPAND_HISTORY_MESSAGES,
        ),
    )


async def _handle_load_active_context(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
) -> JSONResponse:
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        if "scope" in body:
            payload = LoadActiveContextRequest.model_validate(body)
        else:
            auth = _authenticate_for_header_scope(service, api_key)
            parsed = _HeaderBoundLoadActiveContextBody.model_validate(body)
            payload = LoadActiveContextRequest(
                scope=_session_scope_from_headers(request, auth),
                token_budget=parsed.token_budget,
                timezone_name=parsed.timezone_name,
                redaction=parsed.redaction,
            )
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return await _handle_payload(api_key, payload, service.load_active_context)


def _session_scope_from_headers(
    request: Request,
    auth: HostedAuthContext,
    *,
    capability: MemoryCapability = MemoryCapability.FRESH_CONTEXT,
) -> MemoryScope:
    project_id = request.headers.get("x-vexic-project-id")
    if project_id is None or not project_id.strip():
        raise ValueError("X-Vexic-Project-Id header is required.")
    session_id = request.headers.get("x-vexic-session-id")
    if session_id is None or not session_id.strip():
        raise ValueError("X-Vexic-Session-Id header is required.")
    agent_id = request.headers.get("x-vexic-agent-id")
    if agent_id is not None:
        agent_id = agent_id.strip() or None
    return MemoryScope(
        tenant_id=auth.tenant_id,
        project_id=project_id.strip(),
        session_id=session_id.strip(),
        agent_id=agent_id,
        principal=auth.principal,
        trust_boundary=TrustBoundary.NETWORKED,
        capabilities={capability},
    )


class _TriggerDreamPhaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str


async def _handle_trigger_dream_phase(
    request: Request,
    body: dict[str, Any],
    service: HostedMemoryService,
) -> JSONResponse:
    """``POST /v1/trigger_dream_phase`` -- schedule a summarize sweep.

    Body-shape errors (wrong types, unknown fields) are 422, matching the
    other header-bound routes. An unsupported `phase` (v1 hard-restricts to
    `"summarize"`) is a business-rule rejection, not a shape error, so it is
    surfaced as 400 -- deliberately split from the 422 branch below.
    """
    api_key = _api_key(request)
    if api_key is None:
        return _error_response(401, "unauthorized", "Missing hosted API key.")
    try:
        auth = _authenticate_for_header_scope(service, api_key)
        trigger_body = _TriggerDreamPhaseBody.model_validate(body)
    except ValidationError:
        return _error_response(
            422,
            "invalid_request",
            "Request body does not match the Vexic contract.",
        )
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")

    try:
        phase = DreamPhase(trigger_body.phase)
    except ValueError:
        return _error_response(
            400,
            "invalid_request",
            f"Unsupported dream phase: {trigger_body.phase!r}",
        )

    try:
        payload = TriggerDreamPhaseRequest(
            scope=_trigger_dream_phase_scope_from_headers(request, auth),
            phase=phase,
        )
    except ValidationError as exc:
        return _error_response(400, "invalid_request", validation_error_message(exc))
    except ValueError as exc:
        return _value_error_response(exc)

    try:
        result = await service.trigger_dream_phase(api_key, payload)
    except HostedRateLimitExceeded as exc:
        return _error_response(
            429,
            "rate_limited",
            "Hosted rate limit exceeded.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except HostPortNotConfigured:
        return _error_response(503, "host_port_not_configured", "Required host port is not configured.")
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return JSONResponse(result.model_dump(mode="json"), status_code=202)


def _trigger_dream_phase_scope_from_headers(request: Request, auth: HostedAuthContext) -> MemoryScope:
    project_id = request.headers.get("x-vexic-project-id")
    if project_id is None or not project_id.strip():
        raise ValueError("X-Vexic-Project-Id header is required.")
    agent_id = request.headers.get("x-vexic-agent-id")
    if agent_id is not None:
        agent_id = agent_id.strip() or None
    return MemoryScope(
        tenant_id=auth.tenant_id,
        project_id=project_id.strip(),
        agent_id=agent_id,
        principal=auth.principal,
        trust_boundary=TrustBoundary.NETWORKED,
        capabilities={MemoryCapability.DREAM_TRIGGER},
    )


def _authenticate_for_header_scope(
    service: HostedMemoryService,
    api_key: str,
) -> HostedAuthContext:
    authenticate = service.api_keys.authenticate
    return authenticate(api_key)


def _value_error_response(exc: ValueError) -> JSONResponse:
    """Map a ``ValueError`` from a hosted operation to an HTTP response.

    The hosted libSQL/Turso driver raises bare ``ValueError`` for SQL errors
    (ADR 0019), so a storage fault mid-operation must not surface as a
    client-fault 400 or echo the Hrana payload. ``pydantic.ValidationError``
    subclasses ``ValueError`` and can embed marker-like client input in its
    message, so it is classified as a client fault before the storage
    classifiers run and its message is sanitized through
    ``validation_error_message`` rather than dumped raw. Storage detail is
    never logged, mirroring the sanitized control-plane logging convention.
    """
    if isinstance(exc, ValidationError):
        return _error_response(400, "invalid_request", validation_error_message(exc))
    storage_error = _storage_error_response(exc)
    if storage_error is not None:
        return storage_error
    return _error_response(400, "invalid_request", str(exc))


def _storage_error_response(exc: BaseException) -> JSONResponse | None:
    """Return a sanitized response for a recognized storage failure.

    Unlike ``_value_error_response``, this accepts typed sqlite exceptions as
    well as libSQL's bare ``ValueError`` so early auth/setup boundaries cannot
    accidentally downgrade a retryable backend outage to a generic 500.
    """
    if is_retryable_operational_error(exc):
        _log_storage_value_error("retryable_operational", exc)
        return _error_response(
            503,
            "storage_unavailable",
            "Hosted storage is temporarily unavailable.",
        )
    if is_operational_error(exc) or is_unique_violation(exc) or "hrana" in str(exc).lower():
        _log_storage_value_error("operational", exc)
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return None


def _log_storage_value_error(category: str, exc: BaseException) -> None:
    logger.warning(
        "hosted storage error category=%s exception_type=%s",
        category,
        type(exc).__name__,
    )


async def _handle_payload(
    api_key: str,
    payload: _RequestT,
    call: Callable[[str, _RequestT], Awaitable[_ResultT]],
) -> JSONResponse:
    cap_error = _cap_error(payload)
    if cap_error is not None:
        return cap_error
    try:
        result = await call(api_key, payload)
        if isinstance(result, ExpandHistoryResult) and result.truncated and not result.text:
            return _too_large("expand_history range is capped.")
        result = _cap_result(result)
    except HostedRateLimitExceeded as exc:
        return _error_response(
            429,
            "rate_limited",
            "Hosted rate limit exceeded.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except HostPortNotConfigured:
        return _error_response(503, "host_port_not_configured", "Required host port is not configured.")
    except PermissionError as exc:
        if str(exc) == "Invalid hosted API key.":
            return _error_response(401, "unauthorized", "Invalid hosted API key.")
        return _error_response(403, "permission_denied", str(exc))
    except ValueError as exc:
        return _value_error_response(exc)
    except Exception as exc:
        storage_error = _storage_error_response(exc)
        if storage_error is not None:
            return storage_error
        return _error_response(500, "internal_error", "Hosted memory request failed.")
    return JSONResponse(result.model_dump(mode="json"))


def _api_key(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    explicit = request.headers.get("x-vexic-api-key")
    if explicit is not None and explicit.strip():
        return explicit.strip()
    return None


def _cap_error(payload: MemoryRequest) -> JSONResponse | None:
    if isinstance(payload, (SearchTranscriptRequest, SearchLongTermRequest)):
        if len(payload.query) > MAX_QUERY_CHARS:
            return _too_large("query is capped.")
        if payload.limit < 1 or payload.limit > MAX_LIMIT:
            return _error_response(400, "invalid_request", f"limit must be between 1 and {MAX_LIMIT}.")
    if isinstance(payload, ExpandHistoryRequest):
        if payload.first_message_id > payload.last_message_id:
            return _error_response(
                400,
                "invalid_request",
                "first_message_id must be less than or equal to last_message_id.",
            )
    if isinstance(payload, (FreshContextRequest, LoadActiveContextRequest)):
        if not (
            MIN_FRESH_CONTEXT_TOKEN_BUDGET
            <= payload.token_budget
            <= MAX_FRESH_CONTEXT_TOKEN_BUDGET
        ):
            return _error_response(
                400,
                "invalid_request",
                f"token_budget must be between {MIN_FRESH_CONTEXT_TOKEN_BUDGET} and "
                f"{MAX_FRESH_CONTEXT_TOKEN_BUDGET}.",
            )
    return None


def _sweeper_lifespan(
    sweeper: object,
    service: HostedMemoryService,
) -> Callable[[FastAPI], Any]:
    """App lifespan that runs the dream sweeper loop (ADR 0030) beside the
    serving loop and stops it cleanly on shutdown. `sweeper` is any object
    with `async run(stop: asyncio.Event)` -- the `DreamSweeper` in production,
    a double in tests."""
    import asyncio
    from contextlib import asynccontextmanager
    from collections.abc import AsyncIterator

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        task = asyncio.create_task(sweeper.run(stop))  # type: ignore[attr-defined]
        try:
            yield
        finally:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            # Drain in-flight background jobs (dream chains, trigger sweeps,
            # sweep-state recorders) instead of letting loop teardown cancel
            # them mid-write: worker threads cannot be interrupted, and a
            # cancelled orchestrator would otherwise release its scope lock
            # while the thread is still mutating storage.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 60
            while service._background_tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                # Re-snapshot each pass: a finishing job spawns its
                # sweep-state recorder task, which must drain too.
                await asyncio.wait(list(service._background_tasks), timeout=remaining)
            for leftover in list(service._background_tasks):
                leftover.cancel()

    return lifespan


def _cap_result(result: _ResultT) -> _ResultT:
    recap = getattr(result, "recap_text", None)
    if recap is not None and len(recap) > MAX_EXPAND_HISTORY_CHARS:
        # The recap rides outside the token budget (it summarizes spans the
        # budget excluded), so it needs its own hard cap; the original blocks
        # stay recoverable via expand_history.
        result = result.model_copy(
            update={
                "recap_text": recap[:MAX_EXPAND_HISTORY_CHARS],
                "truncated": True,
            }
        )
    if hasattr(result, "text") and len(result.text) > MAX_EXPAND_HISTORY_CHARS:
        update: dict[str, object] = {
            "text": result.text[:MAX_EXPAND_HISTORY_CHARS],
            "truncated": True,
        }
        # The structured fields (e.g. FreshContextResult.recent/summaries)
        # can carry the same raw content as `.text` uncapped. Drop them on
        # truncation so a client can't bypass the char cap via the
        # structured payload -- the original content stays recoverable via
        # expand_history.
        if hasattr(result, "recent"):
            update["recent"] = []
        if hasattr(result, "summaries"):
            update["summaries"] = []
        return result.model_copy(update=update)
    return result


def _too_large(message: str) -> JSONResponse:
    return _error_response(400, "request_too_large", message)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
        headers=headers,
    )


def _root_arg(value: str | None) -> Path:
    return Path(value or os.environ.get("VEXIC_HOSTED_ROOT", ".hosted-memory"))


def _capabilities(values: list[str]) -> set[MemoryCapability]:
    if not values:
        return {MemoryCapability.WRITE, MemoryCapability.SEARCH}
    return {MemoryCapability(value) for value in values}


def main(
    argv: list[str] | None = None,
    *,
    turso_provisioning: _TursoProvisioning | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Manage the Vexic hosted alpha adapter.")
    subcommands = parser.add_subparsers(dest="command", required=True)

    issue = subcommands.add_parser("issue-key")
    issue.add_argument("--root")
    issue.add_argument("--tenant-id", required=True)
    issue.add_argument("--project-id", action="append", default=[])
    issue.add_argument("--principal-id", required=True)
    issue.add_argument("--agent-id", action="append", default=[])
    issue.add_argument("--capability", action="append", default=[])

    revoke = subcommands.add_parser("revoke-key")
    revoke.add_argument("--root")
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--revoked-by")

    add_run_dream_phase_subcommand(subcommands)

    args = parser.parse_args(argv)

    try:
        root = _root_arg(args.root)
        provisioning = turso_provisioning or _TursoProvisioning()
        catalog, keys, customer_target_resolver = _build_hosted_stores(
            root,
            provisioning=provisioning,
            env=os.environ,
            customer_memory=args.command != "revoke-key",
        )

        if args.command == "run-dream-phase":
            return run_dream_phase_command(
                args,
                catalog=catalog,
                keys=keys,
                customer_target_resolver=customer_target_resolver,
            )

        if args.command == "issue-key":
            catalog.provision_tenant(args.tenant_id, project_ids=set(args.project_id))
            api_key = keys.create_key(
                tenant_id=args.tenant_id,
                principal_id=args.principal_id,
                capabilities=_capabilities(args.capability),
                project_ids=set(args.project_id),
                agent_ids=set(args.agent_id),
            )
            print(
                json.dumps(
                    {
                        "key_id": api_key.key_id,
                        "raw_key": api_key.raw_key,
                        "tenant_id": args.tenant_id,
                        "project_ids": args.project_id,
                    },
                    sort_keys=True,
                )
            )
            return 0

        keys.revoke_key(args.key_id, revoked_by=args.revoked_by)
        print(json.dumps({"key_id": args.key_id, "revoked": True}, sort_keys=True))
        return 0
    except (HostPortNotConfigured, PermissionError) as exc:
        parser.exit(2, f"{exc}\n")
    except Exception as exc:
        if is_retryable_operational_error(exc):
            parser.exit(1, "Hosted storage is temporarily unavailable.\n")
        if is_operational_error(exc) or is_unique_violation(exc) or "hrana" in str(exc).lower():
            parser.exit(1, "Hosted command failed due to a storage error.\n")
        if isinstance(exc, ValueError):
            parser.exit(2, f"{exc}\n")
        parser.exit(1, f"hosted command failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
