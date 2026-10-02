"""Outbound HTTP for ingestion.

Public data APIs are shared infrastructure funded by someone else. This client is
deliberately conservative:

* a single serialised request stream with a configurable rate limit;
* a contactable User-Agent, which several publishers (NCBI, Crossref) require as
  a condition of polite use — ingestion refuses to start without a contact email;
* retries with exponential backoff on transport errors and 5xx, honouring
  `Retry-After` on 429;
* **no retry on 4xx other than 429**, because a 403 means we are not permitted
  and hammering it is both rude and useless.

`check_reachability` exists because this system cannot assume it has egress. It
reports what is actually reachable rather than letting the product imply it has
data it has never been able to fetch.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from ..core.config import Settings, get_settings


class IngestionNotPermittedError(RuntimeError):
    """Raised when configuration forbids making outbound requests at all."""


class SourceUnavailableError(RuntimeError):
    """Raised when a source cannot be reached. Never swallowed into empty data.

    This distinction matters: returning an empty result set on a network failure
    would make the product display "no qualifying signals" when the truth is
    "we could not look".
    """

    def __init__(self, host: str, detail: str, status_code: int | None = None) -> None:
        self.host = host
        self.detail = detail
        self.status_code = status_code
        super().__init__(f"{host}: {detail}")


@dataclass(slots=True)
class ReachabilityResult:
    host: str
    url: str
    reachable: bool
    status_code: int | None
    detail: str
    elapsed_ms: float | None = None

    @property
    def verdict(self) -> str:
        if self.reachable:
            return "REACHABLE"
        if self.status_code in (403, 407):
            return "BLOCKED_BY_POLICY"
        if self.status_code is not None:
            return f"HTTP_{self.status_code}"
        return "UNREACHABLE"


class RateLimiter:
    """Minimum-interval limiter. Simple, synchronous, and adequate.

    Ingestion is a batch job, not a latency-sensitive service, so there is no
    reason to introduce concurrency and the attendant risk of exceeding a
    publisher's limits.
    """

    def __init__(self, requests_per_second: float) -> None:
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._last: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last is not None:
            elapsed = now - self._last
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class IngestionHttpClient:
    """The only way ingestion talks to the outside world."""

    def __init__(self, settings: Settings | None = None, *, allow_without_contact: bool = False):
        self.settings = settings or get_settings()
        if not self.settings.http_contact_email and not allow_without_contact:
            raise IngestionNotPermittedError(
                "MR_HTTP_CONTACT_EMAIL is not set. Several public sources require a "
                "contactable User-Agent as a condition of use, so ingestion will not "
                "start without one. Set it in the environment (see ops/.env.example)."
            )
        self.limiter = RateLimiter(self.settings.http_requests_per_second)
        self._client = httpx.Client(
            timeout=self.settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.settings.user_agent()},
        )

    def __enter__(self) -> IngestionHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- requests ----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        host = urlparse(url).netloc
        attempts = self.settings.http_max_retries
        last_detail = "no attempt made"

        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            try:
                response = self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
                if attempt == attempts:
                    raise SourceUnavailableError(host, last_detail) from exc
                time.sleep(2 ** attempt)
                continue

            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if attempt == attempts:
                    raise SourceUnavailableError(
                        host, "rate limited (429) after all retries", 429
                    )
                time.sleep(retry_after if retry_after is not None else 2 ** attempt)
                continue

            if 500 <= response.status_code < 600:
                last_detail = f"server error {response.status_code}"
                if attempt == attempts:
                    raise SourceUnavailableError(host, last_detail, response.status_code)
                time.sleep(2 ** attempt)
                continue

            if 400 <= response.status_code < 500:
                # Not retried: a 403 means we are not permitted, and repeating the
                # request neither helps nor is courteous.
                raise SourceUnavailableError(
                    host,
                    f"client error {response.status_code}: "
                    f"{response.text[:200] if response.text else 'no body'}",
                    response.status_code,
                )

            return response

        raise SourceUnavailableError(host, last_detail)

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.get(url, params=params, headers={"Accept": "application/json"})
        try:
            return response.json()
        except ValueError as exc:
            raise SourceUnavailableError(
                urlparse(url).netloc,
                f"response was not valid JSON: {exc}",
                response.status_code,
            ) from exc

    def get_bytes(self, url: str) -> tuple[bytes, httpx.Response]:
        response = self.get(url)
        return response.content, response

    # -- diagnostics -------------------------------------------------------

    def check_reachability(self, url: str) -> ReachabilityResult:
        """Probe one URL without retries. Used by `moleculeradar sources verify`.

        Deliberately does not raise: the point is to report the truth about every
        host, including the ones that fail.
        """
        host = urlparse(url).netloc
        started = time.monotonic()
        try:
            self.limiter.wait()
            response = self._client.get(url, headers={"Accept": "*/*"})
        except httpx.HTTPError as exc:
            return ReachabilityResult(
                host=host, url=url, reachable=False, status_code=None,
                detail=f"{type(exc).__name__}: {exc}",
                elapsed_ms=(time.monotonic() - started) * 1000,
            )
        elapsed = (time.monotonic() - started) * 1000
        ok = response.status_code < 400
        detail = "ok" if ok else f"HTTP {response.status_code}"
        if response.status_code in (403, 407):
            detail = (
                f"HTTP {response.status_code} — typically an egress policy denial "
                f"rather than a refusal by the publisher. Check the environment's "
                f"network settings and whether {host} is in the allowed domains."
            )
        return ReachabilityResult(
            host=host, url=url, reachable=ok, status_code=response.status_code,
            detail=detail, elapsed_ms=elapsed,
        )


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header, which may be seconds or an HTTP date."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(value)
        delta = (when - dt.datetime.now(dt.UTC)).total_seconds()
        return max(delta, 0.0)
    except (TypeError, ValueError):
        return None
