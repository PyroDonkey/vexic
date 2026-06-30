"""Unit tests for vexic.recorders.hosted_prime (COA-262 SessionStart priming)."""

from __future__ import annotations

import json
import socket
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from vexic.recorders.hosted_prime import (
    DEFAULT_PRIME_MAX_CHARS,
    HostedPrimeConfig,
    _cap,
    _items,
    _post_search,
    _safe_post_search,
    _str,
    build_prime_context,
    fetch_prime_context,
)


def _config(**kwargs) -> HostedPrimeConfig:
    defaults = dict(
        base_url="https://api.example.test/",
        api_key="vx_secret",
        project_id="project-a",
        session_id="session-a",
        agent_id=None,
        timeout_seconds=5.0,
    )
    defaults.update(kwargs)
    return HostedPrimeConfig(**defaults)


class HostedPrimeCapTests(unittest.TestCase):
    """Tests for _cap(), the character-capping helper."""

    def test_cap_returns_text_when_under_limit(self) -> None:
        text = "hello world"
        result = _cap(text, 100)
        self.assertEqual(result, text)

    def test_cap_returns_text_when_exactly_at_limit(self) -> None:
        text = "hello"
        result = _cap(text, 5)
        self.assertEqual(result, text)

    def test_cap_truncates_and_appends_truncated_suffix(self) -> None:
        text = "a" * 30
        result = _cap(text, 20)
        self.assertTrue(result.endswith("\n[truncated]"))
        self.assertLessEqual(len(result), 20)

    def test_cap_returns_empty_string_when_max_chars_zero(self) -> None:
        result = _cap("some text", 0)
        self.assertEqual(result, "")

    def test_cap_returns_empty_string_when_max_chars_negative(self) -> None:
        result = _cap("some text", -1)
        self.assertEqual(result, "")

    def test_cap_falls_back_to_raw_slice_when_max_chars_leq_suffix_length(self) -> None:
        # suffix "\n[truncated]" is 12 chars; if max_chars <= 12 fall back to raw slice
        text = "abcdefghijklmnopqrstuvwxyz"
        result = _cap(text, 10)
        self.assertEqual(result, text[:10])
        self.assertNotIn("[truncated]", result)

    def test_cap_strips_trailing_whitespace_before_appending_suffix(self) -> None:
        # Build a string where the truncation point lands on spaces
        text = "a" * 8 + "   " + "b" * 20
        result = _cap(text, 20)
        self.assertFalse(result[: -len("\n[truncated]")].endswith(" "))
        self.assertTrue(result.endswith("\n[truncated]"))

    def test_cap_empty_string_under_limit(self) -> None:
        self.assertEqual(_cap("", 100), "")


class HostedPrimeItemsTests(unittest.TestCase):
    """Tests for _items(), the safe-list-of-dicts extractor."""

    def test_items_returns_empty_list_for_none(self) -> None:
        self.assertEqual(_items(None), [])

    def test_items_returns_empty_list_for_non_list(self) -> None:
        self.assertEqual(_items("not a list"), [])
        self.assertEqual(_items(42), [])
        self.assertEqual(_items({}), [])

    def test_items_filters_out_non_dicts(self) -> None:
        value = [{"a": 1}, "string", None, 99, {"b": 2}]
        result = _items(value)
        self.assertEqual(result, [{"a": 1}, {"b": 2}])

    def test_items_returns_all_dicts_from_list_of_dicts(self) -> None:
        value = [{"x": 1}, {"y": 2}]
        self.assertEqual(_items(value), value)

    def test_items_returns_empty_list_for_empty_list(self) -> None:
        self.assertEqual(_items([]), [])


class HostedPrimeStrTests(unittest.TestCase):
    """Tests for _str(), the safe-string extractor."""

    def test_str_returns_none_for_none(self) -> None:
        self.assertIsNone(_str(None))

    def test_str_returns_none_for_non_string(self) -> None:
        self.assertIsNone(_str(42))
        self.assertIsNone(_str([]))
        self.assertIsNone(_str({}))

    def test_str_strips_whitespace(self) -> None:
        self.assertEqual(_str("  hello  "), "hello")

    def test_str_returns_none_for_blank_string(self) -> None:
        self.assertIsNone(_str("   "))
        self.assertIsNone(_str(""))

    def test_str_returns_string_for_normal_value(self) -> None:
        self.assertEqual(_str("hello world"), "hello world")


class BuildPrimeContextTests(unittest.TestCase):
    """Tests for build_prime_context()."""

    def test_empty_inputs_return_empty_string(self) -> None:
        result = build_prime_context({}, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertEqual(result, "")

    def test_only_facts_in_long_term(self) -> None:
        long_term = {"facts": [{"fact_text": "User prefers dark mode"}], "candidate_notes": []}
        result = build_prime_context(long_term, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertIn("Vexic memory priming:", result)
        self.assertIn("Long-term memory:", result)
        self.assertIn("User prefers dark mode", result)
        self.assertNotIn("Recent transcript memory:", result)

    def test_only_candidate_notes_in_long_term(self) -> None:
        long_term = {"facts": [], "candidate_notes": [{"fact_text": "tentative cedar fact"}]}
        result = build_prime_context(long_term, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertIn("tentative: tentative cedar fact", result)
        self.assertIn("Long-term memory:", result)

    def test_only_transcript_hits(self) -> None:
        transcript = {"hits": [{"body": "User: remember the cedar rule"}]}
        result = build_prime_context({}, transcript, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertIn("Recent transcript memory:", result)
        self.assertIn("User: remember the cedar rule", result)
        self.assertNotIn("Long-term memory:", result)

    def test_both_long_term_and_transcript(self) -> None:
        long_term = {"facts": [{"fact_text": "Project uses Python"}], "candidate_notes": []}
        transcript = {"hits": [{"body": "User: always use type hints"}]}
        result = build_prime_context(long_term, transcript, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertIn("Long-term memory:", result)
        self.assertIn("Recent transcript memory:", result)
        self.assertIn("Project uses Python", result)
        self.assertIn("always use type hints", result)

    def test_facts_with_empty_fact_text_are_skipped(self) -> None:
        long_term = {
            "facts": [
                {"fact_text": ""},
                {"fact_text": "   "},
                {"fact_text": "Valid fact"},
            ],
            "candidate_notes": [],
        }
        result = build_prime_context(long_term, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertEqual(result.count("- "), 1)  # only one bullet for Valid fact
        self.assertIn("Valid fact", result)

    def test_hits_with_no_body_are_skipped(self) -> None:
        transcript = {
            "hits": [
                {"body": None},
                {"body": "  "},
                {"body": "Real hit"},
            ]
        }
        result = build_prime_context({}, transcript, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertEqual(result.count("- "), 1)
        self.assertIn("Real hit", result)

    def test_result_is_capped_at_max_chars(self) -> None:
        long_term = {"facts": [{"fact_text": "x" * 2000}], "candidate_notes": []}
        transcript = {"hits": [{"body": "y" * 2000}]}
        result = build_prime_context(long_term, transcript, max_chars=500)
        self.assertLessEqual(len(result), 500)

    def test_facts_non_dict_items_are_ignored(self) -> None:
        long_term = {"facts": ["not a dict", 42, {"fact_text": "Good fact"}], "candidate_notes": []}
        result = build_prime_context(long_term, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        self.assertIn("Good fact", result)
        # Should only have one bullet
        self.assertIn("- Good fact", result)

    def test_missing_fact_text_key_is_skipped(self) -> None:
        long_term = {"facts": [{"other_key": "value"}], "candidate_notes": []}
        result = build_prime_context(long_term, {}, max_chars=DEFAULT_PRIME_MAX_CHARS)
        # No facts to show → no Long-term header either
        self.assertEqual(result, "")


class PostSearchTests(unittest.TestCase):
    """Tests for _post_search() and _safe_post_search()."""

    def test_post_search_builds_correct_url_without_trailing_slash(self) -> None:
        config = _config(base_url="https://api.example.test")
        captured: list[tuple[Request, float]] = []

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({"facts": []}).encode()

        def fake_urlopen(req, timeout):
            captured.append((req, timeout))
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = _post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertEqual(len(captured), 1)
        req, timeout = captured[0]
        self.assertEqual(req.full_url, "https://api.example.test/v1/search_long_term")
        self.assertEqual(result, {"facts": []})

    def test_post_search_includes_all_headers_with_agent_id(self) -> None:
        config = _config(agent_id="agent-x")
        captured: list[Request] = []

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({}).encode()

        def fake_urlopen(req, timeout):
            captured.append(req)
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            _post_search(config, "search_transcript", {"query": "q", "limit": 3})

        req = captured[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer vx_secret")
        self.assertEqual(req.get_header("X-vexic-project-id"), "project-a")
        self.assertEqual(req.get_header("X-vexic-session-id"), "session-a")
        self.assertEqual(req.get_header("X-vexic-agent-id"), "agent-x")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_post_search_omits_agent_id_header_when_none(self) -> None:
        config = _config(agent_id=None)
        captured: list[Request] = []

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({}).encode()

        def fake_urlopen(req, timeout):
            captured.append(req)
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            _post_search(config, "search_transcript", {"query": "q", "limit": 3})

        req = captured[0]
        self.assertIsNone(req.get_header("X-vexic-agent-id"))

    def test_post_search_raises_runtime_error_on_http_error(self) -> None:
        config = _config()

        def fake_urlopen(req, timeout):
            raise HTTPError(req.full_url, 401, "Unauthorized", hdrs={}, fp=None)

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                _post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertIn("HTTP 401", str(ctx.exception))

    def test_post_search_raises_runtime_error_on_url_error(self) -> None:
        config = _config()

        def fake_urlopen(req, timeout):
            raise URLError(reason=socket.timeout("timed out"))

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                _post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertIn("hosted prime failed", str(ctx.exception))

    def test_post_search_raises_runtime_error_when_response_is_not_dict(self) -> None:
        config = _config()

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps([1, 2, 3]).encode()

        def fake_urlopen(req, timeout):
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                _post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertIn("invalid response", str(ctx.exception))

    def test_safe_post_search_returns_empty_dict_on_runtime_error(self) -> None:
        config = _config()

        def fake_urlopen(req, timeout):
            raise HTTPError(req.full_url, 500, "Internal Server Error", hdrs={}, fp=None)

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = _safe_post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertEqual(result, {})

    def test_safe_post_search_returns_result_on_success(self) -> None:
        config = _config()
        expected = {"facts": [{"fact_text": "cedar"}], "candidate_notes": []}

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps(expected).encode()

        def fake_urlopen(req, timeout):
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = _safe_post_search(config, "search_long_term", {"query": "q", "limit": 5})

        self.assertEqual(result, expected)

    def test_post_search_sends_correct_payload(self) -> None:
        config = _config()
        captured: list[bytes] = []

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({}).encode()

        def fake_urlopen(req, timeout):
            captured.append(req.data)
            return _Response()

        payload = {"query": "test query", "limit": 3}
        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            _post_search(config, "search_long_term", payload)

        sent = json.loads(captured[0].decode("utf-8"))
        self.assertEqual(sent, payload)


class FetchPrimeContextTests(unittest.TestCase):
    """Tests for fetch_prime_context() including secret redaction."""

    def _make_response(self, payload: dict) -> object:
        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps(payload).encode()
        return _Response()

    def test_fetch_prime_context_returns_empty_when_both_searches_empty(self) -> None:
        config = _config()

        def fake_urlopen(req, timeout):
            return self._make_response({})

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = fetch_prime_context(config)

        self.assertEqual(result, "")

    def test_fetch_prime_context_raises_runtime_error_when_context_contains_api_key(self) -> None:
        config = _config(api_key="super_secret_key")

        def fake_urlopen(req, timeout):
            if "long_term" in req.full_url:
                return self._make_response({})
            return self._make_response(
                {"hits": [{"body": "super_secret_key leaked here"}]}
            )

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_prime_context(config)

        self.assertIn("forbidden secret", str(ctx.exception))

    def test_fetch_prime_context_respects_custom_limits(self) -> None:
        """Verify that custom long_term_limit and transcript_limit are sent in requests."""
        config = _config()
        captured_payloads: list[dict] = []

        class _Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self): return json.dumps({}).encode()

        def fake_urlopen(req, timeout):
            captured_payloads.append(json.loads(req.data.decode()))
            return _Response()

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            fetch_prime_context(config, long_term_limit=2, transcript_limit=3)

        self.assertEqual(len(captured_payloads), 2)
        # search_long_term is called first
        self.assertEqual(captured_payloads[0]["limit"], 2)
        self.assertEqual(captured_payloads[1]["limit"], 3)

    def test_fetch_prime_context_returns_context_when_both_searches_succeed(self) -> None:
        config = _config()

        def fake_urlopen(req, timeout):
            if "long_term" in req.full_url:
                return self._make_response(
                    {"facts": [{"fact_text": "prefer tabs"}], "candidate_notes": []}
                )
            return self._make_response(
                {"hits": [{"body": "User: always run tests"}]}
            )

        with patch("vexic.recorders.hosted_prime.urlopen", fake_urlopen):
            result = fetch_prime_context(config)

        self.assertIn("prefer tabs", result)
        self.assertIn("always run tests", result)
        self.assertNotIn("vx_secret", result)

    def test_fetch_prime_context_default_max_chars(self) -> None:
        """Verify DEFAULT_PRIME_MAX_CHARS is 6000."""
        self.assertEqual(DEFAULT_PRIME_MAX_CHARS, 6_000)