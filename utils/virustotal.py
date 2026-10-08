"""Small rate-limited VirusTotal v3 client for scanning URLs posted in chat."""

import asyncio
import base64
import time
from dataclasses import dataclass, field

import aiohttp

API_BASE = 'https://www.virustotal.com/api/v3'
GUI_BASE = 'https://www.virustotal.com/gui/url'

REPORT_MAX_AGE = 3 * 24 * 3600   # Re-scan URLs whose last report is older than this
RESULT_CACHE_TTL = 6 * 3600      # Reuse our own results for this long
ANALYSIS_POLL_DELAY = 20         # Seconds to wait before checking a fresh analysis
ANALYSIS_MAX_POLLS = 6


@dataclass
class ScanResult:
    url: str
    malicious: int = 0
    suspicious: int = 0
    harmless: int = 0
    undetected: int = 0
    flagged_by: list[str] = field(default_factory=list)

    @property
    def permalink(self) -> str:
        return f"{GUI_BASE}/{url_id(self.url)}"

    @property
    def total(self) -> int:
        return self.malicious + self.suspicious + self.harmless + self.undetected


def url_id(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().strip('=')


class VirusTotalError(Exception):
    pass


class VirusTotalClient:
    def __init__(self, api_key: str, requests_per_minute: int = 4):
        self.api_key = api_key
        self._interval = 60 / max(requests_per_minute, 1)
        self._next_slot = 0.0
        self._slot_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, ScanResult]] = {}
        self._pending: dict[str, asyncio.Future] = {}

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _wait_for_slot(self):
        """Space requests out to stay inside the API quota (4/min on the free tier)."""
        async with self._slot_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def _request(self, method: str, path: str, **kwargs):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={'x-apikey': self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        for attempt in range(2):
            await self._wait_for_slot()
            async with self._session.request(method, API_BASE + path, **kwargs) as resp:
                if resp.status == 429 and attempt == 0:
                    await asyncio.sleep(60)
                    continue
                if resp.status == 404:
                    return None
                if resp.status >= 400:
                    raise VirusTotalError(f"{method} {path} returned HTTP {resp.status}: {(await resp.text())[:200]}")
                return await resp.json()
        raise VirusTotalError("Rate limited by VirusTotal")

    async def scan(self, url: str) -> ScanResult:
        """Scan a URL, reusing a recent result or an in-flight scan of the same URL."""
        cached = self._cache.get(url)
        if cached and cached[0] > time.time():
            return cached[1]
        if url in self._pending:
            return await asyncio.shield(self._pending[url])

        future = asyncio.get_running_loop().create_future()
        self._pending[url] = future
        try:
            result = await self._scan(url)
            self._cache[url] = (time.time() + RESULT_CACHE_TTL, result)
            future.set_result(result)
            return result
        except Exception as e:
            future.set_exception(e)
            future.exception()  # Mark retrieved so waiters-less failures don't warn
            raise
        finally:
            del self._pending[url]
            self._prune_cache()

    async def _scan(self, url: str) -> ScanResult:
        report = await self._request('GET', f"/urls/{url_id(url)}")
        if report:
            attrs = report['data']['attributes']
            if time.time() - attrs.get('last_analysis_date', 0) < REPORT_MAX_AGE:
                return self._parse(url, attrs.get('last_analysis_stats', {}), attrs.get('last_analysis_results', {}))

        submitted = await self._request('POST', '/urls', data={'url': url})
        if not submitted:
            raise VirusTotalError("URL submission failed")
        analysis_id = submitted['data']['id']

        for _ in range(ANALYSIS_MAX_POLLS):
            await asyncio.sleep(ANALYSIS_POLL_DELAY)
            analysis = await self._request('GET', f"/analyses/{analysis_id}")
            if not analysis:
                continue
            attrs = analysis['data']['attributes']
            if attrs.get('status') == 'completed':
                return self._parse(url, attrs.get('stats', {}), attrs.get('results', {}))
        raise VirusTotalError("Analysis did not complete in time")

    @staticmethod
    def _parse(url: str, stats: dict, results: dict) -> ScanResult:
        flagged = sorted(
            name for name, r in results.items()
            if r.get('category') in ('malicious', 'suspicious')
        )
        return ScanResult(
            url=url,
            malicious=stats.get('malicious', 0),
            suspicious=stats.get('suspicious', 0),
            harmless=stats.get('harmless', 0),
            undetected=stats.get('undetected', 0),
            flagged_by=flagged,
        )

    def _prune_cache(self):
        now = time.time()
        for key in [k for k, (expires, _) in self._cache.items() if expires <= now]:
            del self._cache[key]
