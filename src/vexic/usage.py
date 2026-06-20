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
    usage_fn = getattr(result, "usage", None)
    if not callable(usage_fn):
        return UsageSummary()
    usage = usage_fn()
    return UsageSummary(
        model_requests=int(getattr(usage, "requests", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        # Pricing is provider/model-specific and the repo does not maintain a
        # price table yet. Keep the audit column explicit and truthful.
        estimated_cost_micros=0,
    )
