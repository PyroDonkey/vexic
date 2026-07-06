import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from vexic.recorders.setup_exchange import (
    SetupExchangeConfig,
    SetupExchangeResult,
    exchange_setup_token,
)


class SetupExchangeTests(unittest.TestCase):
    def test_exchange_maps_response_fields_and_posts_token(self) -> None:
        calls = []

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "apiKey": "vx_exchanged",
                        "keyId": "key-1",
                        "projectId": "project-a",
                        "sessionId": "session-a",
                        "agentId": "agent-a",
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return _Response()

        config = SetupExchangeConfig(base_url="https://api.example.test/", timeout_seconds=7.0)

        with patch("vexic.recorders.setup_exchange.urlopen", fake_urlopen):
            result = exchange_setup_token(config, token="vxsetup_abc")

        self.assertEqual(
            result,
            SetupExchangeResult(
                api_key="vx_exchanged",
                key_id="key-1",
                project_id="project-a",
                session_id="session-a",
                agent_id="agent-a",
            ),
        )
        request, timeout = calls[0]
        self.assertEqual(timeout, 7.0)
        self.assertEqual(request.full_url, "https://api.example.test/v1/setup/exchange")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data.decode()), {"token": "vxsetup_abc"})

    def test_exchange_allows_null_agent_id(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "apiKey": "vx_exchanged",
                        "keyId": "key-1",
                        "projectId": "project-a",
                        "sessionId": "session-a",
                        "agentId": None,
                    }
                ).encode("utf-8")

        config = SetupExchangeConfig(base_url="https://api.example.test")

        with patch("vexic.recorders.setup_exchange.urlopen", lambda request, timeout: _Response()):
            result = exchange_setup_token(config, token="vxsetup_abc")

        self.assertIsNone(result.agent_id)

    def test_exchange_401_raises_actionable_message_without_token(self) -> None:
        config = SetupExchangeConfig(base_url="https://api.example.test")
        error = HTTPError(
            url="https://api.example.test/v1/setup/exchange",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )

        with patch("vexic.recorders.setup_exchange.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                exchange_setup_token(config, token="vxsetup_secret")

        message = str(caught.exception)
        self.assertIn("already used, expired, or revoked", message)
        self.assertIn("mint a new token", message)
        self.assertNotIn("vxsetup_secret", message)

    def test_exchange_non_401_http_error_is_sanitized(self) -> None:
        config = SetupExchangeConfig(base_url="https://api.example.test")
        error = HTTPError(
            url="https://api.example.test/v1/setup/exchange",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )

        with patch("vexic.recorders.setup_exchange.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "setup token exchange failed: HTTP 500"):
                exchange_setup_token(config, token="vxsetup_secret")

    def test_exchange_url_error_is_sanitized(self) -> None:
        config = SetupExchangeConfig(base_url="https://api.example.test")
        error = URLError(ConnectionRefusedError("refused"))

        with patch("vexic.recorders.setup_exchange.urlopen", side_effect=error):
            with self.assertRaisesRegex(
                RuntimeError, "setup token exchange failed: ConnectionRefusedError"
            ):
                exchange_setup_token(config, token="vxsetup_secret")

    def test_exchange_rejects_malformed_response(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                # Missing apiKey — a partial/malformed server response must not
                # surface a raw KeyError; it becomes a sanitized RuntimeError.
                return json.dumps(
                    {
                        "keyId": "key-1",
                        "projectId": "project-a",
                        "sessionId": "session-a",
                        "agentId": None,
                    }
                ).encode("utf-8")

        config = SetupExchangeConfig(base_url="https://api.example.test")

        with patch(
            "vexic.recorders.setup_exchange.urlopen",
            lambda request, timeout: _Response(),
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed response"):
                exchange_setup_token(config, token="vxsetup_secret")

    def test_exchange_rejects_non_object_response(self) -> None:
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self) -> bytes:
                return json.dumps(["not", "an", "object"]).encode("utf-8")

        config = SetupExchangeConfig(base_url="https://api.example.test")

        with patch(
            "vexic.recorders.setup_exchange.urlopen",
            lambda request, timeout: _Response(),
        ):
            with self.assertRaisesRegex(RuntimeError, "malformed response"):
                exchange_setup_token(config, token="vxsetup_secret")

    def test_exchange_rejects_non_http_base_url(self) -> None:
        config = SetupExchangeConfig(base_url="file:///tmp/vexic")

        with patch("vexic.recorders.setup_exchange.urlopen") as urlopen_mock:
            with self.assertRaisesRegex(ValueError, "base_url.*http"):
                exchange_setup_token(config, token="vxsetup_secret")

        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
