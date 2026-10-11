"""Chainlink REST Transport Adapter for boundary queries and Discovery."""
from __future__ import annotations

from typing import Callable, Optional
from urllib.parse import urlparse
import httpx

from polymarket_bot.market_data.models import MarketDataError
from polymarket_bot.market_data.numeric import load_numeric_json
from polymarket_bot.recorder.chainlink_auth import (
    build_chainlink_auth_headers,
    validate_credentials,
)


class ChainlinkRestClient:
    """Credential-isolated HTTP client for Chainlink Data Streams REST endpoints.

    Only the following hosts are permitted:
      - api.dataengine.chain.link (REST API)
      - ws.dataengine.chain.link (WebSocket, used by ChainlinkLiveConsumer)

    All requests are signed with HMAC. No Polymarket credentials or routes
    are ever accessible from this client.
    """

    _REST_HOST = "api.dataengine.chain.link"
    _WS_HOST = "ws.dataengine.chain.link"
    _MAX_RESPONSE_BYTES = 10 * 1024 * 1024

    def __init__(
        self,
        client: httpx.Client,
        *,
        client_id: str,
        client_secret: str,
        clock_seconds: Callable[[], int],
        feed_id: str,
    ):
        self._client = client
        self._client_id, self._client_secret = validate_credentials(client_id, client_secret)
        self._clock_seconds = clock_seconds
        self._feed_id = feed_id

    def _validate_host(self, url: str) -> str:
        parsed = urlparse(url)
        allowed_hosts = {self._REST_HOST, self._WS_HOST}
        if parsed.netloc.lower() not in allowed_hosts:
            raise MarketDataError("invalid_host", f"Host '{parsed.netloc}' is not a permitted Chainlink endpoint.")
        return url

    def _build_signed_headers(self, method: str, path: str, body: Optional[bytes] = None) -> dict[str, str]:
        timestamp_ms = self._clock_seconds() * 1000
        return build_chainlink_auth_headers(
            client_id=self._client_id,
            client_secret=self._client_secret,
            method=method,
            path=path,
            body=body,
            timestamp_ms=timestamp_ms,
        )

    def _get(self, base_url: str, path: str, *, deadline: float, params: dict | None = None,
             expect_single_report: bool = True) -> tuple[bytes, dict]:
        self._validate_host(base_url)
        url = base_url.rstrip("/") + path
        headers = self._build_signed_headers("GET", path)
        timeout = httpx.Timeout(deadline)
        try:
            response = self._client.get(url, headers=headers, params=params, timeout=timeout)
            return self._handle_response(response, path, expect_single_report=expect_single_report)
        except httpx.TimeoutException as exc:
            raise MarketDataError("chainlink_timeout", f"Chainlink {path} timed out.") from exc
        except httpx.NetworkError as exc:
            raise MarketDataError("chainlink_network_error", f"Chainlink {path} network error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MarketDataError("chainlink_http_error", f"Chainlink {path} HTTP error: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise MarketDataError("chainlink_timeout", f"Chainlink {path} timed out.") from exc
        except httpx.NetworkError as exc:
            raise MarketDataError("chainlink_network_error", f"Chainlink {path} network error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MarketDataError("chainlink_http_error", f"Chainlink {path} HTTP error: {exc}") from exc

    def _post(self, base_url: str, path: str, *, deadline: float, json_body: dict) -> tuple[bytes, dict]:
        self._validate_host(base_url)
        url = base_url.rstrip("/") + path
        body_bytes = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
        headers = self._build_signed_headers("POST", path, body=body_bytes)
        headers["Content-Type"] = "application/json"
        timeout = httpx.Timeout(deadline)
        try:
            response = self._client.post(url, headers=headers, content=body_bytes, timeout=timeout)
            return self._handle_response(response, path)
        except httpx.TimeoutException as exc:
            raise MarketDataError("chainlink_timeout", f"Chainlink {path} timed out.") from exc
        except httpx.NetworkError as exc:
            raise MarketDataError("chainlink_network_error", f"Chainlink {path} network error: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MarketDataError("chainlink_http_error", f"Chainlink {path} HTTP error: {exc}") from exc

    def _handle_response(self, response: httpx.Response, path: str, *,
                         expect_single_report: bool = True) -> tuple[bytes, dict]:
        status = response.status_code
        if status == 401 or status == 403:
            raise MarketDataError("chainlink_auth_failed", f"Chainlink {path} authentication failed (HTTP {status}).", http_status=status)
        if status == 429:
            raise MarketDataError("chainlink_rate_limited", f"Chainlink {path} returned HTTP {status}.", http_status=status)
        if 500 <= status <= 599:
            raise MarketDataError("chainlink_http_error", f"Chainlink {path} returned HTTP {status}.", http_status=status)
        if status != 200:
            raise MarketDataError("chainlink_http_error", f"Chainlink {path} returned HTTP {status}.", http_status=status)

        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._MAX_RESPONSE_BYTES:
                    raise MarketDataError("response_too_large", f"Chainlink {path} response exceeds 10 MiB.")
            except ValueError:
                pass

        raw_body = response.read()
        if len(raw_body) > self._MAX_RESPONSE_BYTES:
            raise MarketDataError("response_too_large", f"Chainlink {path} response exceeds 10 MiB.")

        try:
            payload = load_numeric_json(raw_body)
        except MarketDataError as exc:
            raise MarketDataError("invalid_response", f"Chainlink {path} returned invalid JSON.") from exc

        if not isinstance(payload, dict):
            raise MarketDataError("invalid_response", f"Chainlink {path} response must be an object.")

        if expect_single_report:
            report = payload.get("report", payload)
            if not isinstance(report, dict):
                raise MarketDataError("invalid_response", f"Chainlink {path} response missing report object.")

            full_report_hex = report.get("fullReport") or report.get("full_report")
            if not isinstance(full_report_hex, str) or not full_report_hex:
                raise MarketDataError("invalid_response", f"Chainlink {path} missing fullReport field.")

            full_report_hex = full_report_hex.strip()
            if full_report_hex.startswith("0x") or full_report_hex.startswith("0X"):
                full_report_hex = full_report_hex[2:]

            try:
                raw_report = bytes.fromhex(full_report_hex)
            except ValueError as exc:
                raise MarketDataError("invalid_response", f"Chainlink {path} fullReport is not valid hex.") from exc

            return raw_report, report
        else:
            return raw_body, payload

    def get_latest_report(self, deadline: float) -> tuple[bytes, dict]:
        """Fetch the latest signed report for the configured feed."""
        return self._get(f"https://{self._REST_HOST}", "/api/v1/reports/latest", deadline=deadline, params={"feedId": self._feed_id})

    def get_report_at_timestamp(self, timestamp_s: int, deadline: float) -> tuple[bytes, dict]:
        """Fetch a report at or near the given Unix timestamp (seconds)."""
        if not isinstance(timestamp_s, int) or timestamp_s < 0:
            raise MarketDataError("invalid_timestamp", "Timestamp must be a non-negative integer.")
        return self._get(f"https://{self._REST_HOST}", "/api/v1/reports", deadline=deadline, params={"feedId": self._feed_id, "timestamp": str(timestamp_s)})

    def get_reports_page(self, start_s: int, end_s: int, limit: int, deadline: float) -> list[tuple[bytes, dict]]:
        """Fetch a bounded page of reports within [start_s, end_s]."""
        if not (isinstance(start_s, int) and isinstance(end_s, int) and start_s >= 0 and end_s >= start_s):
            raise MarketDataError("invalid_timestamp", "Invalid start/end timestamps.")
        if not isinstance(limit, int) or not (1 <= limit <= 100):
            raise MarketDataError("invalid_limit", "Limit must be an integer between 1 and 100.")
        raw_body, payload = self._get(f"https://{self._REST_HOST}", "/api/v1/reports/page",
                                       deadline=deadline,
                                       params={"feedId": self._feed_id, "start": str(start_s), "end": str(end_s), "limit": str(limit)},
                                       expect_single_report=False)
        if "reports" not in payload or not isinstance(payload["reports"], list):
            raise MarketDataError("invalid_response", "Page response missing 'reports' list.")
        results = []
        for report_obj in payload["reports"]:
            if not isinstance(report_obj, dict):
                continue
            if "report" in report_obj and isinstance(report_obj["report"], dict):
                report_obj = report_obj["report"]
            full_report_hex = report_obj.get("fullReport") or report_obj.get("full_report")
            if not isinstance(full_report_hex, str) or not full_report_hex:
                continue
            full_report_hex = full_report_hex.strip()
            if full_report_hex.startswith("0x") or full_report_hex.startswith("0X"):
                full_report_hex = full_report_hex[2:]
            try:
                raw_report = bytes.fromhex(full_report_hex)
            except ValueError:
                continue
            results.append((raw_report, report_obj))
        return results