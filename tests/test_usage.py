"""Behavior pins for vexic.usage.summarize_agent_usage (COA-375).

pydantic-ai is migrating AgentRunResult.usage from a method to a property.
These tests pin that both shapes yield real token telemetry and that a
result exposing no usage fails loud instead of recording zeros.
"""

import unittest
from types import SimpleNamespace

from vexic.usage import summarize_agent_usage


def _usage_payload() -> SimpleNamespace:
    return SimpleNamespace(
        requests=2,
        input_tokens=100,
        output_tokens=40,
        total_tokens=140,
    )


class SummarizeAgentUsageTest(unittest.TestCase):
    def test_property_form_usage_is_captured(self) -> None:
        result = SimpleNamespace(usage=_usage_payload())

        summary = summarize_agent_usage(result)

        self.assertEqual(summary.model_requests, 2)
        self.assertEqual(summary.input_tokens, 100)
        self.assertEqual(summary.output_tokens, 40)
        self.assertEqual(summary.total_tokens, 140)

    def test_callable_form_usage_is_captured(self) -> None:
        result = SimpleNamespace(usage=_usage_payload)

        summary = summarize_agent_usage(result)

        self.assertEqual(summary.model_requests, 2)
        self.assertEqual(summary.input_tokens, 100)
        self.assertEqual(summary.output_tokens, 40)
        self.assertEqual(summary.total_tokens, 140)

    def test_none_token_fields_coerce_to_zero(self) -> None:
        result = SimpleNamespace(
            usage=SimpleNamespace(
                requests=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
            )
        )

        summary = summarize_agent_usage(result)

        self.assertEqual(summary.model_requests, 0)
        self.assertEqual(summary.total_tokens, 0)

    def test_pinned_pydantic_ai_usage_shape_is_known(self) -> None:
        """A pydantic-ai bump that changes AgentRunResult.usage again should
        break this test, not the billing counter."""
        from pydantic_ai.agent import AgentRunResult

        attr = AgentRunResult.usage
        is_property = isinstance(attr, property)
        is_known_shim = type(attr).__name__ == "_DeprecatedCallableProperty"
        self.assertTrue(
            is_property or is_known_shim,
            f"unexpected AgentRunResult.usage shape: {type(attr)!r}",
        )

    def test_result_without_usage_fails_loud(self) -> None:
        with self.assertRaises(ValueError):
            summarize_agent_usage(object())

    def test_none_usage_fails_loud(self) -> None:
        with self.assertRaises(ValueError):
            summarize_agent_usage(SimpleNamespace(usage=None))


if __name__ == "__main__":
    unittest.main()
