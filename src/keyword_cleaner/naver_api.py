from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import requests
from dotenv import load_dotenv


class NaverSearchAdsError(RuntimeError):
    pass


@dataclass(frozen=True)
class NaverConfig:
    api_key: str
    secret_key: str
    customer_id: str
    base_url: str = "https://api.searchad.naver.com"

    @classmethod
    def from_env(cls) -> "NaverConfig":
        load_dotenv()
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
        timestamp = str(int(time.time() * 1000))
        return {
            "X-Timestamp": timestamp,
            "X-API-KEY": self.config.api_key,
            "X-Customer": self.config.customer_id,
            "X-Signature": self._signature(
                timestamp, method, uri, self.config.secret_key
            ),
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
        }

    def _throttle(self) -> None:
        wait = self.min_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def _request(
        self,
        method: str,
        uri: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        json_body: Any = None,
        allow_empty: bool = False,
    ) -> Any:
        url = self.config.base_url + uri
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=params,
                    json=json_body,
                    headers=self._headers(method, uri),
                    timeout=self.timeout,
                )
                self._last_request_at = time.monotonic()

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < self.max_retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = (
                            float(retry_after)
                            if retry_after
                            and retry_after.replace(".", "", 1).isdigit()
                            else min(2**attempt, 20)
                        )
                        time.sleep(delay)
                        continue

                if not response.ok:
                    try:
                        detail = response.json()
                    except ValueError:
                        detail = response.text[:1000]
                    raise NaverSearchAdsError(
                        f"{method.upper()} {uri} failed "
                        f"({response.status_code}): {detail}"
                    )

                if allow_empty and not response.content:
                    return None
                if not response.content:
                    return None
                return response.json()

            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2**attempt, 20))

        raise NaverSearchAdsError(
            f"{method.upper()} {uri} failed after retries: {last_error}"
        )

    def _paged_get(
        self,
        uri: str,
        *,
        params: Mapping[str, Any] | None,
        id_field: str,
        record_size: int = 1000,
    ) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        base_search_id: str | None = None

        while True:
            page_params: dict[str, Any] = dict(params or {})
            page_params["recordSize"] = record_size
            page_params["selector"] = "NEXT"
            if base_search_id:
                page_params["baseSearchId"] = base_search_id

            rows = self._request("GET", uri, params=page_params)
            if not isinstance(rows, list):
                raise NaverSearchAdsError(
                    f"Unexpected response from {uri}: expected list, "
                    f"got {type(rows).__name__}"
                )
            if not rows:
                break

            all_rows.extend(rows)
            if len(rows) < record_size:
                break

            next_base = str(rows[-1].get(id_field, "")).strip()
            if not next_base or next_base == base_search_id:
                break
            base_search_id = next_base

        return all_rows

    def get_campaigns(self) -> list[dict[str, Any]]:
        return self._paged_get(
            "/ncc/campaigns", params=None, id_field="nccCampaignId"
        )

    def get_adgroups(self, campaign_id: str) -> list[dict[str, Any]]:
        return self._paged_get(
            "/ncc/adgroups",
            params={"nccCampaignId": campaign_id},
            id_field="nccAdgroupId",
        )

    def get_keywords(self, adgroup_id: str) -> list[dict[str, Any]]:
        return self._paged_get(
            "/ncc/keywords",
            params={"nccAdgroupId": adgroup_id},
            id_field="nccKeywordId",
        )

    def get_keyword(self, keyword_id: str) -> dict[str, Any]:
        data = self._request("GET", f"/ncc/keywords/{keyword_id}")
        if not isinstance(data, dict):
            raise NaverSearchAdsError(
                f"Unexpected keyword response for {keyword_id}"
            )
        return data

    def get_keyword_stats(
        self,
        keyword_ids: Sequence[str],
        *,
        since: str,
        until: str,
        batch_size: int = 100,
    ) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {
            keyword_id: {"impCnt": 0, "clkCnt": 0}
            for keyword_id in keyword_ids
        }
        fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
        time_range = json.dumps(
            {"since": since, "until": until}, separators=(",", ":")
        )

        for batch in _chunks(list(keyword_ids), batch_size):
            params: list[tuple[str, Any]] = [
                ("ids", keyword_id) for keyword_id in batch
            ]
            params.extend(
                [
                    ("fields", fields),
                    ("timeRange", time_range),
                ]
            )
            payload = self._request("GET", "/stats", params=params)
            if isinstance(payload, dict):
                rows = payload.get("data", [])
            elif isinstance(payload, list):
                rows = payload
            else:
                rows = []

            for row in rows:
                if not isinstance(row, dict):
                    continue
                keyword_id = str(row.get("id", "")).strip()
                if keyword_id in result:
                    result[keyword_id] = {
                        "impCnt": int(float(row.get("impCnt", 0) or 0)),
                        "clkCnt": int(float(row.get("clkCnt", 0) or 0)),
                    }

        return result

    def delete_keyword(self, keyword_id: str) -> None:
        self._request(
            "DELETE",
            f"/ncc/keywords/{keyword_id}",
            allow_empty=True,
        )

    def create_keyword_from_backup(
        self, adgroup_id: str, backup: Mapping[str, Any]
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"keyword": backup["keyword"]}

        for key in ("bidAmt", "useGroupBidAmt", "links", "attr"):
            if key in backup and backup[key] is not None:
                body[key] = backup[key]

        payload = self._request(
            "POST",
            "/ncc/keywords",
            params={"nccAdgroupId": adgroup_id},
            json_body=[body],
        )
        if not isinstance(payload, list) or not payload:
            raise NaverSearchAdsError(
                "Unexpected response while restoring keyword"
            )
        return payload[0]
