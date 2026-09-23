from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests
from dotenv import load_dotenv


class NaverSearchAdsError(RuntimeError):
    pass


def _load_portable_env() -> None:
    """Load .env from the portable app folder first, then fall back normally."""
    candidates: list[Path] = []
    configured_root = os.getenv("DAYLAW_APP_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root) / ".env")
    candidates.append(Path.cwd() / ".env")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / ".env")

    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            load_dotenv(dotenv_path=path, override=True)
            return

    load_dotenv()


@dataclass(frozen=True)
class NaverConfig:
    api_key: str
    secret_key: str
    customer_id: str
    base_url: str = "https://api.searchad.naver.com"

    @classmethod
    def from_env(cls) -> "NaverConfig":
        _load_portable_env()
        api_key = os.getenv("NAVER_API_KEY", "").strip()
        secret_key = os.getenv("NAVER_SECRET_KEY", "").strip()
        customer_id = os.getenv("NAVER_CUSTOMER_ID", "").strip()
        base_url = os.getenv(
            "NAVER_BASE_URL", "https://api.searchad.naver.com"
        ).rstrip("/")

        missing = [
            name
            for name, value in (
                ("NAVER_API_KEY", api_key),
                ("NAVER_SECRET_KEY", secret_key),
                ("NAVER_CUSTOMER_ID", customer_id),
            )
            if not value
        ]
        if missing:
            raise NaverSearchAdsError(
                "Missing environment variables: " + ", ".join(missing)
            )
        return cls(api_key, secret_key, customer_id, base_url)


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class NaverSearchAdsClient:
    def __init__(
        self,
        config: NaverConfig,
        *,
        timeout: int = 30,
        max_retries: int = 5,
        min_interval_seconds: float = 0.05,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval_seconds = min_interval_seconds
        self.session = requests.Session()
        self._last_request_at = 0.0
        # Some Windows machines can have a small system-clock skew. Naver rejects
        # signed requests whose timestamp falls outside its acceptance window.
        # When that happens we learn the offset from the HTTP Date header and retry.
        self._clock_offset_ms = 0.0

    @staticmethod
    def _signature(timestamp: str, method: str, uri: str, secret_key: str) -> str:
        message = f"{timestamp}.{method.upper()}.{uri}"
        digest = hmac.new(
            secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, uri: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000 + self._clock_offset_ms))
        return {
            "X-Timestamp": timestamp,
            "X-API-KEY": self.config.api_key,
            "X-Customer": self.config.customer_id,
            "X-Signature": self._signature(
                timestamp, method, uri, self.config.secret_key
            ),
            "Content-Type": "application/json; charset=UTF-8",
        }

    @staticmethod
    def _is_timestamp_error(response: requests.Response) -> bool:
        if response.status_code != 403:
            return False
        text = response.text.lower()
        return (
            "invalid timestamp" in text
            or "request has expired" in text
            or "invalidtimestamp" in text
        )

    def _sync_clock_from_response(self, response: requests.Response) -> bool:
        """Adjust request timestamps to the API server's HTTP Date header."""
        date_header = response.headers.get("Date", "").strip()
        if not date_header:
            return False
        try:
            server_dt = parsedate_to_datetime(date_header)
            if server_dt.tzinfo is None:
                return False
            # HTTP Date only has one-second precision. Aim near the middle of that
            # second to avoid sitting exactly on the previous-second boundary.
            server_ms = server_dt.timestamp() * 1000 + 500
            local_ms = time.time() * 1000
            self._clock_offset_ms = server_ms - local_ms
            return True
        except Exception:
            return False

    def _request(
        self,
        method: str,
        uri: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = self.config.base_url + uri
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < self.min_interval_seconds:
                time.sleep(self.min_interval_seconds - elapsed)
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(method, uri),
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()
                if response.status_code >= 400:
                    detail = response.text
                    try:
                        detail = json.dumps(response.json(), ensure_ascii=False)
                    except Exception:
                        pass
                    error = NaverSearchAdsError(
                        f"{method} {uri} failed ({response.status_code}): {detail}"
                    )

                    if self._is_timestamp_error(response):
                        last_error = error
                        if attempt < self.max_retries - 1 and self._sync_clock_from_response(response):
                            time.sleep(0.05)
                            continue
                        raise NaverSearchAdsError(
                            str(error)
                            + " | Windows 시간 동기화가 필요할 수 있습니다. "
                            + "설정 > 시간 및 언어 > 날짜 및 시간 > 지금 동기화를 실행해주세요."
                        ) from error

                    if response.status_code in {429, 500, 502, 503, 504}:
                        last_error = error
                        time.sleep(min(2 ** attempt, 8))
                        continue
                    raise error
                if not response.content:
                    return None
                try:
                    return response.json()
                except ValueError:
                    return response.text
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 8))
        if last_error is not None:
            raise NaverSearchAdsError(str(last_error)) from last_error
        raise NaverSearchAdsError(f"{method} {uri} failed without response")

    def get_campaigns(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/ncc/campaigns")
        return list(result or [])

    def get_adgroups(self, campaign_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET", "/ncc/adgroups", params={"nccCampaignId": campaign_id}
        )
        return list(result or [])

    def get_keywords(self, adgroup_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "GET", "/ncc/keywords", params={"nccAdgroupId": adgroup_id}
        )
        return list(result or [])

    def get_keyword(self, keyword_id: str) -> dict[str, Any]:
        result = self._request("GET", f"/ncc/keywords/{keyword_id}")
        return dict(result or {})

    def get_stats(
        self,
        ids: Sequence[str],
        *,
        fields: Sequence[str],
        since: str,
        until: str,
        time_increment: int = 1,
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        all_rows: list[dict[str, Any]] = []
        params = {
            "ids": ",".join(ids),
            "fields": json.dumps(list(fields), ensure_ascii=False),
            "timeRange": json.dumps({"since": since, "until": until}),
            "timeIncrement": time_increment,
        }
        result = self._request("GET", "/stats", params=params)
        if isinstance(result, list):
            all_rows.extend(result)
        return all_rows

    def delete_keyword(self, keyword_id: str) -> None:
        self._request("DELETE", f"/ncc/keywords/{keyword_id}")

    def create_keyword(
        self,
        *,
        adgroup_id: str,
        keyword: str,
        bid_amt: int | None = None,
        user_lock: bool | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "nccAdgroupId": adgroup_id,
            "keyword": keyword,
        }
        if bid_amt is not None:
            body["bidAmt"] = int(bid_amt)
        if user_lock is not None:
            body["userLock"] = bool(user_lock)
        result = self._request("POST", "/ncc/keywords", json_body=[body])
        if isinstance(result, list) and result:
            return dict(result[0])
        if isinstance(result, dict):
            return dict(result)
        return {}
