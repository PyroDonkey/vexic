from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class HostPortNotConfigured(RuntimeError):
    """Raised when a model-backed operation is called without a host adapter."""


class AgentLike(Protocol):
    async def run(self, prompt: str, *args: Any, **kwargs: Any) -> Any: ...


class AgentFactory(Protocol):
    def __call__(
        self,
        model_group: str,
        secrets: Mapping[str, str] | None = None,
    ) -> AgentLike: ...


type EmbedTexts = Callable[[list[str]], list[list[float]]]


class ContentCodec(Protocol):
    """Encode canonical content before storage; decode after reads (ADR 0023).

    No codec means plaintext passthrough -- the local default. Hosted
    adapters supply an encrypting codec. Codecs own their envelope: encoded
    values carry a codec-specific version prefix and ``decode`` passes
    through values without it, so legacy plaintext rows keep reading
    correctly during migration. AAD binding (table/column context) is fixed
    at codec construction, not per call, and key material never lives in
    ``src/vexic``.
    """

    def encode(self, plaintext: str) -> str: ...

    def decode(self, stored: str) -> str: ...


@dataclass(frozen=True)
class DreamPhasePorts:
    model_group: str
    embed: EmbedTexts | None = None
    extraction_agent_factory: AgentFactory | None = None
    contradiction_agent_factory: AgentFactory | None = None
    summary_agent_factory: AgentFactory | None = None
    defer_contradiction: bool = True
    secrets: Mapping[str, str] | None = None
    daily_span_budget: int = 50


def missing_host_port(name: str, hint: str | None = None) -> HostPortNotConfigured:
    message = (
        f"{name} requires a host-supplied model port. "
        "Vexic core does not read provider secrets or build models directly."
    )
    if hint:
        message = f"{message} {hint}"
    return HostPortNotConfigured(message)
