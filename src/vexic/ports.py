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


@dataclass(frozen=True)
class DreamPhasePorts:
    model_group: str
    embed: EmbedTexts | None = None
    extraction_agent_factory: AgentFactory | None = None
    rem_agent_factory: AgentFactory | None = None
    contradiction_agent_factory: AgentFactory | None = None
    secrets: Mapping[str, str] | None = None


def missing_host_port(name: str) -> HostPortNotConfigured:
    return HostPortNotConfigured(
        f"{name} requires a host-supplied model port. "
        "Vexic core does not read provider secrets or build models directly."
    )
