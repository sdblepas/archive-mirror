"""
Discovers all items in an Internet Archive collection via the scrape API.

The scrape API supports cursor-based pagination and handles collections
larger than the 10 000-row limit of the legacy advancedsearch endpoint.

API reference:
  GET https://archive.org/services/search/v1/scrape
      ?q=collection:{collection}
      &fields=identifier,title,date,creator,subject
      &count=1000
      &cursor={cursor}          ← omit on first request
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

import httpx

from .config import Config
from .logger import get_logger

log = get_logger(__name__)

_SCRAPE_URL = "https://archive.org/services/search/v1/scrape"
_FIELDS = "identifier,title,date,creator,subject,description"


class DiscoveryError(Exception):
    pass


class Discoverer:
    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self._cfg = config
        self._client = client

    async def iter_items(self) -> AsyncIterator[dict]:
        """Yield every item dict in the collection, handling pagination."""
        cursor: Optional[str] = None
        page = 0
        total_yielded = 0

        while True:
            params: dict = {
                "q": f"collection:{self._cfg.collection}",
                "fields": _FIELDS,
                "count": 1000,
            }
            if cursor:
                params["cursor"] = cursor

            log.info(
                "discovery.fetch_page",
                collection=self._cfg.collection,
                page=page,
                cursor=cursor,
            )

            data = await self._fetch_with_retry(params)
            items = data.get("items", [])

            if not items:
                log.info("discovery.no_more_items", total=total_yielded)
                return

            for item in items:
                yield item
                total_yielded += 1

            # Cursor absent or null → last page
            cursor = data.get("cursor")
            if not cursor:
                log.info(
                    "discovery.complete",
                    total=total_yielded,
                    pages=page + 1,
                )
                return

            page += 1
            # Be a good citizen: brief pause between discovery pages
            await asyncio.sleep(self._cfg.rate_limit_delay)

    async def _fetch_with_retry(self, params: dict) -> dict:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._cfg.retry_count + 2):
            try:
                response = await self._client.get(
                    _SCRAPE_URL,
                    params=params,
                    timeout=self._cfg.request_timeout,
                )
                if response.status_code == 429:
                    wait = 30 * attempt
                    log.warning(
                        "discovery.rate_limited",
                        attempt=attempt,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, Exception) as exc:
                last_exc = exc
                if attempt <= self._cfg.retry_count:
                    wait = min(4 * 2 ** (attempt - 1), 120)
                    log.warning(
                        "discovery.fetch_error",
                        attempt=attempt,
                        error=str(exc),
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)

        raise DiscoveryError(
            f"Failed to fetch discovery page after {self._cfg.retry_count + 1} attempts"
        ) from last_exc
