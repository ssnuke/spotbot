from __future__ import annotations

import time
from typing import Optional

import requests

_CACHE_TTL_SECONDS = 3600
_cached_rate: Optional[float] = None
_cached_at: float = 0.0


def get_usd_inr_rate() -> Optional[float]:
    """Returns the current USD->INR rate, cached for an hour to avoid hammering the
    API. USDT is treated as pegged 1:1 to USD — a reasonable display approximation,
    not an exact conversion. Returns None (falling back to a stale cache if one
    exists) if the rate can't be fetched."""
    global _cached_rate, _cached_at
    now = time.time()
    if _cached_rate is not None and now - _cached_at < _CACHE_TTL_SECONDS:
        return _cached_rate

    try:
        response = requests.get(
            "https://api.frankfurter.app/latest", params={"from": "USD", "to": "INR"}, timeout=10
        )
        response.raise_for_status()
        rate = float(response.json()["rates"]["INR"])
        _cached_rate = rate
        _cached_at = now
        return rate
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return _cached_rate
