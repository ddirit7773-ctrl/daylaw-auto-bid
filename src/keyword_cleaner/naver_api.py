from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests


@dataclass
class NaverSearchAdsClient:
    api_key: str
    secret_key: str
    customer_id: str
    base_url: str = "https://api.searchad.naver.com"
    timeout: int = 30

    def _signature(self, timestamp: str, method: str, uri: str) -> str:
        message = f"{timestamp}.{method}.{uri}"
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _headers(self, method: str, uri: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "X-Timestamp": timestamp,
            "X-API-KEY": self.api_key,
            "X-Customer": str(self.customer_id),
            "X-Signature": self._signature(timestamp, method, uri),
            "Content-Type": "application/json; charset=UTF-8",
        }

    def request(
        self,
        method: str,
        uri: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        method = method.upper()
        url = f"{self.base_url}{uri}"
        response = requests.request(
            method,
            url,
            headers=self._headers(method, uri),
            params=params,
            json=json,
            timeout=self.timeout,
        )
        if not response.ok:
            detail = response.text[:1000]
            raise RuntimeError(
                f"NAVER API error {response.status_code} {method} {uri}: {detail}"
            )
        if not response.content:
            return None
        return response.json()

    def get_campaigns(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/ncc/campaigns")
        return data or []

    def get_adgroups(self, campaign_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            "/ncc/adgroups",
            params={"nccCampaignId": campaign_id},
        )
        return data or []

    def get_keywords(self, adgroup_id: str) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            "/ncc/keywords",
            params={"nccAdgroupId": adgroup_id},
        )
        return data or []
