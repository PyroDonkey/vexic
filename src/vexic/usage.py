from dataclasses import dataclass


@dataclass(frozen=True)
class UsageSummary:
    model_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_micros: int = 0

    def plus(self, other: "UsageSummary") -> "UsageSummary":
        return UsageSummary(
            model_requests=self.model_requests + other.model_requests,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            estimated_cost_micros=self.estimated_cost_micros + other.estimated_cost_micros,
        )


def summarize_agent_usage(result: object) -> UsageSummary:
    usage = getattr(result, "usage", None)
    # pydantic-ai >=1.102 deprecates the method form of AgentRunResult.usage.
    # The transitional shim is a RunUsage that is *also* callable (calling it
    # emits the deprecation warning), so prefer reading token fields off the
    # object and invoke only a true method-form accessor.
    if callable(usage) and not hasattr(usage, "total_tokens"):
        usage = usage()
    try:
        requests = usage.requests
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        total_tokens = usage.total_tokens
    except AttributeError as exc:
        # A silent all-zero summary would read as "this run used no tokens"
        # in dream_runs and billing telemetry; refuse instead.
        raise ValueError(
            f"agent result exposes no usable usage payload: {usage!r}"
        ) from exc
    return UsageSummary(
        model_requests=int(requests or 0),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        total_tokens=int(total_tokens or 0),
        # Pricing is provider/model-specific and the repo does not maintain a
        # price table yet. Keep the audit column explicit and truthful.
        estimated_cost_micros=0,
    )
