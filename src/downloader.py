"""
Async file downloader with:
  - HTTP Range-header resume for partial downloads
  - MD5 / SHA-1 checksum validation
  - Configurable retry with exponential back-off
  - .part staging + os.replace() atomic promotion
  - Semaphore-limited concurrency
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import aiofiles
import httpx

from .config import Config
from .logger import get_logger

log = get_logger(__name__)

_IA_DOWNLOAD = "https://archive.org/download/{identifier}/{filename}"
_CHUNK = 256 * 1024  # 256 KiB


class DownloadResult(Enum):
    DOWNLOADED = auto()
    SKIPPED_EXISTING = auto()
    FAILED = auto()


@dataclass
class DownloadOutcome:
    result: DownloadResult
    local_path: Optional[Path] = None
    error: Optional[str] = None
    bytes_written: int = 0


class Downloader:
    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self._cfg = config
        self._client = client
        self._sem = asyncio.Semaphore(self._cfg.max_workers)

    async def download_track(
        self,
        *,
        identifier: str,
        ia_filename: str,
        dest_dir: Path,
        local_filename: str,
        expected_size: Optional[int] = None,
        expected_md5: Optional[str] = None,
        expected_sha1: Optional[str] = None,
    ) -> DownloadOutcome:
        async with self._sem:
            return await self._download(
                identifier=identifier,
                ia_filename=ia_filename,
                dest_dir=dest_dir,
                local_filename=local_filename,
                expected_size=expected_size,
                expected_md5=expected_md5,
                expected_sha1=expected_sha1,
            )

    async def _download(
        self,
        *,
        identifier: str,
        ia_filename: str,
        dest_dir: Path,
        local_filename: str,
        expected_size: Optional[int],
        expected_md5: Optional[str],
        expected_sha1: Optional[str],
    ) -> DownloadOutcome:
        dest_path = dest_dir / local_filename
        part_path = dest_dir / (local_filename + ".part")
        url = _IA_DOWNLOAD.format(identifier=identifier, filename=ia_filename)

        # ── 1. Check if file is already complete ─────────────────────────
        if dest_path.exists():
            if _is_valid(dest_path, expected_size, expected_md5, expected_sha1):
                log.debug(
                    "download.skip_existing",
                    identifier=identifier,
                    filename=local_filename,
                )
                return DownloadOutcome(
                    result=DownloadResult.SKIPPED_EXISTING,
                    local_path=dest_path,
                )
            log.warning(
                "download.existing_invalid",
                identifier=identifier,
                filename=local_filename,
                reason="size/checksum mismatch — redownloading",
            )
            dest_path.unlink(missing_ok=True)

        # ── 2. Determine resume offset ────────────────────────────────────
        resume_offset = 0
        if part_path.exists():
            resume_offset = part_path.stat().st_size
            if expected_size and resume_offset >= expected_size:
                part_path.unlink()
                resume_offset = 0

        # ── 3. Download with retry ────────────────────────────────────────
        last_error: Optional[str] = None
        for attempt in range(1, self._cfg.retry_count + 2):
            try:
                outcome = await self._attempt_download(
                    url=url,
                    part_path=part_path,
                    dest_path=dest_path,
                    resume_offset=resume_offset,
                    expected_size=expected_size,
                    expected_md5=expected_md5,
                    expected_sha1=expected_sha1,
                    identifier=identifier,
                    filename=local_filename,
                )
                await asyncio.sleep(self._cfg.rate_limit_delay)
                return outcome
            except _RetryableError as exc:
                last_error = str(exc)
                if attempt <= self._cfg.retry_count:
                    wait = min(4 * 2 ** (attempt - 1), 300)
                    log.warning(
                        "download.retry",
                        identifier=identifier,
                        filename=local_filename,
                        attempt=attempt,
                        error=last_error,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                    if part_path.exists():
                        resume_offset = part_path.stat().st_size
            except _FatalError as exc:
                last_error = str(exc)
                break

        log.error(
            "download.failed",
            identifier=identifier,
            filename=local_filename,
            error=last_error,
        )
        return DownloadOutcome(result=DownloadResult.FAILED, error=last_error)

    async def _attempt_download(
        self,
        *,
        url: str,
        part_path: Path,
        dest_path: Path,
        resume_offset: int,
        expected_size: Optional[int],
        expected_md5: Optional[str],
        expected_sha1: Optional[str],
        identifier: str,
        filename: str,
    ) -> DownloadOutcome:
        headers: dict[str, str] = {}
        if resume_offset > 0:
            headers["Range"] = f"bytes={resume_offset}-"

        try:
            async with self._client.stream(
                "GET",
                url,
                headers=headers,
                timeout=self._cfg.request_timeout,
                follow_redirects=True,
            ) as response:
                if response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", "60"))
                    await asyncio.sleep(wait)
                    raise _RetryableError("429 rate limited")

                if response.status_code in (404, 403):
                    raise _FatalError(f"HTTP {response.status_code} for {url}")

                if response.status_code not in (200, 206):
                    raise _RetryableError(f"HTTP {response.status_code} for {url}")

                # ── BUG FIX: server ignored Range header → reset everything ──
                server_ignored_range = (
                    response.status_code == 200 and resume_offset > 0
                )
                if server_ignored_range:
                    resume_offset = 0
                    part_path.unlink(missing_ok=True)

                # Initialise hash objects AFTER knowing whether we're resuming
                md5_h = hashlib.md5()
                sha1_h = hashlib.sha1()

                # Pre-hash existing bytes only when server honoured the Range header
                if resume_offset > 0 and part_path.exists():
                    async with aiofiles.open(part_path, "rb") as pf:
                        while chunk := await pf.read(_CHUNK):
                            md5_h.update(chunk)
                            sha1_h.update(chunk)

                mode = "ab" if resume_offset > 0 else "wb"
                bytes_written = 0
                part_path.parent.mkdir(parents=True, exist_ok=True)

                async with aiofiles.open(part_path, mode) as out:
                    async for chunk in response.aiter_bytes(chunk_size=_CHUNK):
                        await out.write(chunk)
                        md5_h.update(chunk)
                        sha1_h.update(chunk)
                        bytes_written += len(chunk)

        except (
            httpx.RemoteProtocolError,
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.ReadError,
            httpx.ConnectError,
            OSError,
        ) as exc:
            raise _RetryableError(str(exc)) from exc

        total_bytes = resume_offset + bytes_written
        log.info(
            "download.complete",
            identifier=identifier,
            filename=filename,
            bytes=total_bytes,
        )

        # ── Checksum validation ───────────────────────────────────────────
        actual_md5 = md5_h.hexdigest()
        actual_sha1 = sha1_h.hexdigest()

        if expected_md5 and actual_md5 != expected_md5:
            part_path.unlink(missing_ok=True)
            raise _RetryableError(
                f"MD5 mismatch: expected {expected_md5}, got {actual_md5}"
            )
        if expected_sha1 and actual_sha1 != expected_sha1:
            part_path.unlink(missing_ok=True)
            raise _RetryableError(
                f"SHA1 mismatch: expected {expected_sha1}, got {actual_sha1}"
            )
        if expected_size and total_bytes != expected_size:
            part_path.unlink(missing_ok=True)
            raise _RetryableError(
                f"Size mismatch: expected {expected_size}, got {total_bytes}"
            )

        # ── Atomic promotion .part → final (os.replace is POSIX-atomic) ──
        os.replace(str(part_path), str(dest_path))

        return DownloadOutcome(
            result=DownloadResult.DOWNLOADED,
            local_path=dest_path,
            bytes_written=total_bytes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_valid(
    path: Path,
    expected_size: Optional[int],
    expected_md5: Optional[str],
    expected_sha1: Optional[str],
) -> bool:
    try:
        actual_size = path.stat().st_size
    except OSError:
        return False

    if expected_size is not None and actual_size != expected_size:
        return False

    if expected_md5 or expected_sha1:
        md5_h = hashlib.md5()
        sha1_h = hashlib.sha1()
        try:
            with open(path, "rb") as f:
                while chunk := f.read(_CHUNK):
                    md5_h.update(chunk)
                    sha1_h.update(chunk)
        except OSError:
            return False
        if expected_md5 and md5_h.hexdigest() != expected_md5:
            return False
        if expected_sha1 and sha1_h.hexdigest() != expected_sha1:
            return False

    return True


async def write_checksum_manifest(
    folder: Path,
    tracks: list[tuple[str, str]],
) -> None:
    manifest_path = folder / "checksums.md5"
    lines = [f"{md5}  {name}\n" for name, md5 in sorted(tracks)]
    async with aiofiles.open(manifest_path, "w") as f:
        await f.writelines(lines)


class _RetryableError(Exception):
    pass


class _FatalError(Exception):
    pass
