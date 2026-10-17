"""Focused offline coverage for centralized classification and retry budgets."""
import os
import sys
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("PERSONALAGENT_MONITOR_FILE", "tests/.monitoring-test.json")

from retry import (
    AccountUploadLimitError,
    AuthError,
    ExternalWriteOutcomeUnknownError,
    ModelNotFoundError,
    ModelConfigurationError,
    NonRetryableAPIError,
    RetryExhaustedError,
    UploadOutcomeUnknownError,
    api_timeout_seconds,
    build_google_service,
    call_google_write,
    classify_error,
    create_groq_client,
    retry_download,
    retry_google_read,
    retry_groq,
    retry_upload,
)


class FakeAPIError(Exception):
    def __init__(self, status, message="API failure", reason=None, headers=None):
        super().__init__(message)
        self.status_code = status
        self.response = types.SimpleNamespace(headers=headers or {})
        self.error_details = [{"reason": reason}] if reason else []


class RetryClassificationTests(unittest.TestCase):
    def test_account_upload_limit_stops_immediately_even_with_legacy_guard_off(self):
        calls = []

        def fail():
            calls.append(1)
            raise FakeAPIError(400, "limit reached", reason="uploadLimitExceeded")

        with patch.dict(os.environ, {"UPLOAD_LIMIT_GUARD_ENABLED": "false"}), patch("retry.time.sleep") as sleep:
            with self.assertRaises(AccountUploadLimitError):
                retry_upload(fail)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_permanent_http_errors_do_not_retry(self):
        calls = []

        def fail():
            calls.append(1)
            raise FakeAPIError(400, "invalid title", reason="invalidTitle")

        with patch("retry.time.sleep") as sleep:
            with self.assertRaises(NonRetryableAPIError):
                retry_upload(fail)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_model_not_found_auth_and_transient_network_are_distinct(self):
        self.assertEqual(classify_error(FakeAPIError(404, "model does not exist"), service="groq").category,
                         "model_not_found")
        self.assertEqual(classify_error(FakeAPIError(401, "invalid api key"), service="groq").category,
                         "authentication")
        self.assertEqual(classify_error(ConnectionError("connection reset"), service="groq").category,
                         "network")

    def test_model_not_found_and_auth_fail_without_retries(self):
        cases = [
            (FakeAPIError(404, "model does not exist"), ModelNotFoundError),
            (FakeAPIError(401, "invalid api key"), AuthError),
        ]
        for failure, expected in cases:
            calls = []

            def fail():
                calls.append(1)
                raise failure

            with self.subTest(expected=expected.__name__), patch("retry.time.sleep") as sleep:
                with self.assertRaises(expected):
                    retry_groq(fail)
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_groq_restricted_models_and_invalid_requests_are_not_retried(self):
        cases = [
            (FakeAPIError(403, "model restricted for this account"), ModelConfigurationError),
            (FakeAPIError(400, "unsupported request parameter"), NonRetryableAPIError),
        ]
        for failure, expected in cases:
            calls = []
            def fail():
                calls.append(1)
                raise failure
            with self.subTest(expected=expected.__name__), patch("retry.time.sleep") as sleep:
                with self.assertRaises(expected):
                    retry_groq(fail)
                self.assertEqual(len(calls), 1)
                sleep.assert_not_called()

    def test_transient_provider_and_google_read_failures_are_bounded(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise FakeAPIError(503, "server unavailable")
            return "ok"

        with patch("retry.time.sleep") as sleep:
            self.assertEqual(retry_groq(flaky), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

        read_calls = []

        def flaky_read():
            read_calls.append(1)
            if len(read_calls) == 1:
                raise FakeAPIError(503, "server unavailable")
            return {"items": []}

        with patch("retry.time.sleep") as sleep:
            self.assertEqual(retry_google_read(flaky_read), {"items": []})
        self.assertEqual(len(read_calls), 2)
        sleep.assert_called_once()

    def test_transient_exhaustion_stops_at_configured_attempt_count(self):
        calls = []

        def fail():
            calls.append(1)
            raise TimeoutError("API timeout")

        with patch("retry.time.sleep"):
            with self.assertRaises(RetryExhaustedError):
                retry_groq(fail)
        self.assertEqual(len(calls), 4)

    def test_groq_retry_limit_is_configurable(self):
        calls = []

        def fail():
            calls.append(1)
            raise TimeoutError("temporary network timeout")

        with patch("config.GROQ_MAX_RETRIES", 1), patch("retry.time.sleep") as sleep:
            with self.assertRaises(RetryExhaustedError):
                retry_groq(fail)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()

    def test_retry_after_is_respected_and_capped(self):
        calls = []

        def rate_limited_once():
            calls.append(1)
            if len(calls) == 1:
                raise FakeAPIError(429, "rate limited", headers={"retry-after": "2"})
            return "ok"

        with patch("retry.time.sleep") as sleep, patch("retry.random.uniform", return_value=0):
            self.assertEqual(retry_groq(rate_limited_once), "ok")
        sleep.assert_called_once_with(2.0)

    def test_provider_and_google_clients_use_explicit_timeout_and_no_hidden_retries(self):
        captured = {}
        groq_module = types.ModuleType("groq")
        client_instances = []
        def fake_groq(**kwargs):
            captured["groq"] = kwargs
            client = object()
            client_instances.append(client)
            return client
        groq_module.Groq = fake_groq
        create_groq_client.cache_clear()
        with patch.dict(sys.modules, {"groq": groq_module}), \
             patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "12"}):
            first_client = create_groq_client("test-key-reusable")
            second_client = create_groq_client("test-key-reusable")
            self.assertIs(first_client, second_client)
            self.assertEqual(captured["groq"], {"api_key": "test-key-reusable", "timeout": 12.0, "max_retries": 0})
            self.assertEqual(api_timeout_seconds(), 12.0)
        self.assertEqual(len(client_instances), 1)
        create_groq_client.cache_clear()

        http_module = types.ModuleType("httplib2")
        http_module.Http = lambda **kwargs: captured.update(http=kwargs) or "transport"
        auth_http_module = types.ModuleType("google_auth_httplib2")
        auth_http_module.AuthorizedHttp = lambda credentials, http: (credentials, http)
        api_package = types.ModuleType("googleapiclient")
        api_package.__path__ = []
        discovery_module = types.ModuleType("googleapiclient.discovery")
        discovery_module.build = lambda *args, **kwargs: captured.update(build=(args, kwargs)) or "service"
        with patch.dict(sys.modules, {
            "httplib2": http_module,
            "google_auth_httplib2": auth_http_module,
            "googleapiclient": api_package,
            "googleapiclient.discovery": discovery_module,
        }), patch.dict(os.environ, {"API_TIMEOUT_SECONDS": "12"}):
            self.assertEqual(build_google_service("youtube", "v3", "credentials"), "service")
        self.assertEqual(captured["http"], {"timeout": 12.0})
        self.assertEqual(captured["build"][0], ("youtube", "v3"))

    def test_upload_transport_failure_and_write_interruption_are_not_retried(self):
        upload_calls = []

        def timeout():
            upload_calls.append(1)
            raise TimeoutError("socket timed out")

        with patch("retry.time.sleep"):
            with self.assertRaises(UploadOutcomeUnknownError):
                retry_upload(timeout)
        self.assertEqual(len(upload_calls), 1)

        with self.assertRaises(ExternalWriteOutcomeUnknownError):
            call_google_write(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), task="comment insert")

    def test_permanent_source_download_does_not_retry(self):
        calls = []

        def fail():
            calls.append(1)
            raise RuntimeError("Private video")

        with patch("retry.time.sleep"):
            from retry import PermanentSourceError
            with self.assertRaises(PermanentSourceError):
                retry_download(fail)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
