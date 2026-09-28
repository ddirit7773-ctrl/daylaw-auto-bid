from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Sequence

from .naver_api import NaverSearchAdsClient, NaverSearchAdsError


@dataclass(frozen=True)
class VerifiedKeywordStat:
    keyword_id: str
    impressions: int | None
    clicks: int | None
    complete: bool
    source: str
    error: str | None = None


def _parse_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        rows = payload.get("data", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _to_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _chunks(values: Sequence[str], size: int) -> list[Sequence[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _emit_batch_progress(done: int, total: int) -> None:
    """Emit a machine-readable line for the packaged GUI.

    We intentionally report only about forty times per stats query so a
    90k-keyword account does not flood the UI thread.
    """
    if total <= 0:
        return
    report_every = max(1, total // 40)
    if done == 1 or done == total or done % report_every == 0:
        print(f"@@STATS_PROGRESS|{done}|{total}", flush=True)


def _safe_worker_count(requested: int | None = None) -> int:
    """Return conservative read-only stats concurrency.

    The default is deliberately small. Naver retry/backoff remains active in
    every worker, and operators can reduce it to 1 without changing code.
    """
    if requested is not None:
        value = requested
    else:
        try:
            value = int(os.getenv("DAYLAW_STATS_WORKERS", "3").strip() or "3")
        except ValueError:
            value = 3
    return max(1, min(value, 4))


def _materialize_batch_result(
    batch: Sequence[str],
    payload: object,
) -> dict[str, VerifiedKeywordStat]:
    rows = _parse_rows(payload)
    batch_set = set(batch)
    returned: dict[str, tuple[int, int]] = {}
    for row in rows:
        keyword_id = str(row.get("id", "")).strip()
        if not keyword_id or keyword_id not in batch_set:
            continue
        returned[keyword_id] = (
            _to_int(row.get("impCnt")),
            _to_int(row.get("clkCnt")),
        )

    out: dict[str, VerifiedKeywordStat] = {}
    for keyword_id in batch:
        if keyword_id in returned:
            imp, clk = returned[keyword_id]
            out[keyword_id] = VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=imp,
                clicks=clk,
                complete=True,
                source="multi_returned",
            )
        else:
            out[keyword_id] = VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=0,
                clicks=0,
                complete=True,
                source="multi_omitted_zero",
            )
    return out


def _error_batch_result(
    batch: Sequence[str],
    exc: Exception,
    *,
    source: str,
) -> dict[str, VerifiedKeywordStat]:
    return {
        keyword_id: VerifiedKeywordStat(
            keyword_id=keyword_id,
            impressions=None,
            clicks=None,
            complete=False,
            source=source,
            error=str(exc),
        )
        for keyword_id in batch
    }


def get_verified_keyword_stats(
    client: NaverSearchAdsClient,
    keyword_ids: Sequence[str],
    *,
    since: str,
    until: str,
    batch_size: int = 100,
    max_workers: int | None = None,
) -> dict[str, VerifiedKeywordStat]:
    """Fetch keyword stats without converting request failures into zero.

    Safe-fast behavior:
    - ids remain split into the same conservative 100-id batches by default;
    - up to three read-only batches are fetched concurrently;
    - each worker owns its own requests.Session/Naver client;
    - Naver's existing retry/backoff logic remains active per worker;
    - any failed batch remains DATA_INSUFFICIENT rather than being treated as 0.

    Progress is emitted as ``@@STATS_PROGRESS|done_batches|total_batches`` so
    the Windows desktop app can show a live progress bar and estimated time.
    """
    clean_ids = [str(value).strip() for value in keyword_ids if str(value).strip()]
    result: dict[str, VerifiedKeywordStat] = {}

    if not clean_ids:
        return result

    batch_size = max(1, min(int(batch_size), 100))
    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    batches = _chunks(clean_ids, batch_size)
    total_batches = len(batches)
    workers = _safe_worker_count(max_workers)

    print(
        f"@@FAST_STATS|workers={workers}|batch_size={batch_size}|batches={total_batches}",
        flush=True,
    )

    def params_for(batch: Sequence[str]) -> dict[str, object]:
        return {
            "ids": list(batch),
            "fields": fields,
            "timeRange": time_range,
            "timeIncrement": "allDays",
        }

    if workers == 1 or total_batches <= 1:
        for batch_index, batch in enumerate(batches, start=1):
            try:
                payload = client._request("GET", "/stats", params=params_for(batch))
                result.update(_materialize_batch_result(batch, payload))
            except NaverSearchAdsError as exc:
                result.update(_error_batch_result(batch, exc, source="batch_error"))
            _emit_batch_progress(batch_index, total_batches)
        return result

    worker_local = threading.local()

    def worker(batch: Sequence[str]) -> dict[str, VerifiedKeywordStat]:
        worker_client = getattr(worker_local, "client", None)
        if worker_client is None:
            # Separate Session per thread: requests.Session is not shared across
            # threads. Keep a small per-worker interval and the existing 429/
            # 5xx exponential backoff from NaverSearchAdsClient.
            worker_client = NaverSearchAdsClient(
                client.config,
                timeout=client.timeout,
                max_retries=client.max_retries,
                min_interval_seconds=max(client.min_interval_seconds, 0.10),
            )
            worker_local.client = worker_client
        try:
            payload = worker_client._request("GET", "/stats", params=params_for(batch))
            return _materialize_batch_result(batch, payload)
        except NaverSearchAdsError as exc:
            return _error_batch_result(batch, exc, source="batch_error")
        except Exception as exc:  # defensive: never turn worker failure into zero
            return _error_batch_result(batch, exc, source="batch_worker_error")

    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="daylaw-stats") as pool:
        future_to_batch = {pool.submit(worker, batch): batch for batch in batches}
        for future in as_completed(future_to_batch):
            batch = future_to_batch[future]
            try:
                batch_result = future.result()
            except Exception as exc:  # should be unreachable due worker guard
                batch_result = _error_batch_result(
                    batch,
                    exc,
                    source="batch_future_error",
                )
            result.update(batch_result)
            completed += 1
            _emit_batch_progress(completed, total_batches)

    return result


def get_singular_verified_keyword_stat(
    client: NaverSearchAdsClient,
    keyword_id: str,
    *,
    since: str,
    until: str,
) -> VerifiedKeywordStat:
    """Single-keyword verification for the final delete gate.

    Final deletion verification deliberately stays singular and sequential.
    Speed optimizations are limited to read-only bulk scanning.
    """
    keyword_id = str(keyword_id).strip()
    if not keyword_id:
        return VerifiedKeywordStat(
            keyword_id="",
            impressions=None,
            clicks=None,
            complete=False,
            source="invalid_id",
            error="empty keyword id",
        )

    fields = json.dumps(["impCnt", "clkCnt"], separators=(",", ":"))
    time_range = json.dumps({"since": since, "until": until}, separators=(",", ":"))
    params = {
        "id": keyword_id,
        "fields": fields,
        "timeRange": time_range,
        "timeIncrement": "allDays",
    }

    try:
        payload = client._request("GET", "/stats", params=params)
    except NaverSearchAdsError as exc:
        return VerifiedKeywordStat(
            keyword_id=keyword_id,
            impressions=None,
            clicks=None,
            complete=False,
            source="singular_error",
            error=str(exc),
        )

    rows = _parse_rows(payload)
    if not rows:
        try:
            current = client.get_keyword(keyword_id)
        except NaverSearchAdsError as exc:
            return VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=None,
                clicks=None,
                complete=False,
                source="singular_empty_keyword_unverified",
                error=str(exc),
            )

        current_id = str(current.get("nccKeywordId", "")).strip()
        if current_id != keyword_id:
            return VerifiedKeywordStat(
                keyword_id=keyword_id,
                impressions=None,
                clicks=None,
                complete=False,
                source="singular_empty_keyword_mismatch",
                error=f"keyword lookup returned {current_id!r}",
            )

        return VerifiedKeywordStat(
            keyword_id=keyword_id,
            impressions=0,
            clicks=0,
            complete=True,
            source="singular_empty_zero_keyword_exists",
        )

    impressions = sum(_to_int(row.get("impCnt")) for row in rows)
    clicks = sum(_to_int(row.get("clkCnt")) for row in rows)
    return VerifiedKeywordStat(
        keyword_id=keyword_id,
        impressions=impressions,
        clicks=clicks,
        complete=True,
        source="singular_verified",
    )
