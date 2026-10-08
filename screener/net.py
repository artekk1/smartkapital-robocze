"""Pobieranie HTTP z cache na dysku. Każda odpowiedź ma zapisany URL i czas pobrania,
żeby każdą liczbę w raporcie dało się przypisać do źródła i daty."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Minimalny odstęp między zapytaniami do hosta (sekundy). SEC dopuszcza 10 req/s.
MIN_INTERVAL = {
    "sec.gov": 0.12,
    "finance.yahoo.com": 0.4,
    "nasdaq.com": 0.4,
    "finviz.com": 1.2,
}


class FetchError(RuntimeError):
    pass


class NotFound(FetchError):
    pass


@dataclass
class Doc:
    url: str
    retrieved_at: str  # ISO 8601, UTC
    data: Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Fetcher:
    def __init__(self, cache_dir: Path, sec_user_agent: str | None,
                 max_age_h: float = 20.0, offline: bool = False):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sec_ua = sec_user_agent
        self.max_age_h = max_age_h
        self.offline = offline
        self.session = requests.Session()
        self._last: dict[str, float] = {}

    def _headers(self, host: str) -> dict[str, str]:
        if host.endswith("sec.gov"):
            if not self.sec_ua:
                raise FetchError("SEC wymaga nagłówka User-Agent z kontaktem: "
                                 "ustaw SEC_USER_AGENT='Imię Nazwisko email@domena'")
            return {"User-Agent": self.sec_ua, "Accept-Encoding": "gzip, deflate"}
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
        if host.endswith("nasdaq.com"):
            headers.update({"Accept": "application/json, text/plain, */*",
                            "Origin": "https://www.nasdaq.com",
                            "Referer": "https://www.nasdaq.com/"})
        return headers

    def _throttle(self, host: str) -> None:
        for suffix, gap in MIN_INTERVAL.items():
            if host == suffix or host.endswith("." + suffix):
                wait = self._last.get(suffix, 0.0) + gap - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last[suffix] = time.monotonic()
                return

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def get(self, url: str, as_json: bool = True,
            transform: Callable[[Any], Any] | None = None) -> Doc:
        """Pobiera URL. `transform` przycina odpowiedź przed zapisem do cache
        (np. companyfacts ma kilka MB, a potrzebujemy kilkudziesięciu tagów)."""
        path = self._cache_path(url)
        if path.exists():
            rec = json.loads(path.read_text())
            age_h = (_now() - datetime.fromisoformat(rec["retrieved_at"])).total_seconds() / 3600
            if rec.get("not_found"):
                if self.offline or age_h <= self.max_age_h:
                    raise NotFound(url)
            elif self.offline or age_h <= self.max_age_h:
                return Doc(rec["url"], rec["retrieved_at"], rec["data"])
        if self.offline:
            raise FetchError(f"brak w cache (tryb offline): {url}")

        host = urlparse(url).hostname or ""
        err: Exception | None = None
        for attempt in range(4):
            self._throttle(host)
            try:
                resp = self.session.get(url, headers=self._headers(host), timeout=40)
            except requests.RequestException as e:
                err = e
                time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                err = FetchError(f"HTTP {resp.status_code}")
                time.sleep(2 ** (attempt + 1))
                continue
            retrieved = _now().isoformat(timespec="seconds")
            if resp.status_code == 404:
                path.write_text(json.dumps({"url": url, "retrieved_at": retrieved, "not_found": True}))
                raise NotFound(url)
            if resp.status_code != 200:
                raise FetchError(f"HTTP {resp.status_code}: {url}")
            if as_json:
                try:
                    data = resp.json()
                except ValueError as e:
                    raise FetchError(f"niepoprawny JSON z {url}: {e}") from e
            else:
                data = resp.text
            if transform is not None:
                data = transform(data)
            path.write_text(json.dumps({"url": url, "retrieved_at": retrieved, "data": data}))
            return Doc(url, retrieved, data)
        raise FetchError(f"{url}: {err}")
