from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from vexic.url_policy import require_http_url


@dataclass(frozen=True)
class SetupExchangeConfig:
    base_url: str
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class SetupExchangeResult:
    api_key: str
    key_id: str
    project_id: str
    session_id: str
    agent_id: str | None


def exchange_setup_token(config: SetupExchangeConfig, *, token: str) -> SetupExchangeResult:
    base_url = require_http_url("base_url", config.base_url)
    request = Request(
        urljoin(base_url + "/", "v1/setup/exchange"),
        data=json.dumps({"token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.timeout_seconds) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError(
                "setup token rejected: it may be already used, expired, or revoked "
                "— mint a new token in the console"
            ) from exc
        raise RuntimeError(f"setup token exchange failed: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"setup token exchange failed: {type(exc.reason).__name__}"
        ) from exc

    if not isinstance(body, dict):
        raise RuntimeError("setup token exchange returned a malformed response")

    fields: dict[str, str] = {}
    for wire_name in ("apiKey", "keyId", "projectId", "sessionId"):
        value = body.get(wire_name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("setup token exchange returned a malformed response")
        fields[wire_name] = value

    agent_id = body.get("agentId")
    if agent_id is not None and not isinstance(agent_id, str):
        raise RuntimeError("setup token exchange returned a malformed response")
    if isinstance(agent_id, str) and not agent_id.strip():
        agent_id = None

    return SetupExchangeResult(
        api_key=fields["apiKey"],
        key_id=fields["keyId"],
        project_id=fields["projectId"],
        session_id=fields["sessionId"],
        agent_id=agent_id,
    )
