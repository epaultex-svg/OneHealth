"""Reverse geocoding: GPS coordinates -> human-readable city.

Uses OpenStreetMap Nominatim. Free, no API key. Per Nominatim usage policy we
send a descriptive User-Agent and cap volume by caching the resolved city at
write time (see tools.store_info / tools.resolve_location_city); this module is
only the raw network call. Any failure (timeout, non-200, no result, malformed
body) returns None so callers fail soft and never surface coordinates.
"""

from __future__ import annotations

import httpx

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "OneHealthBot/1.0 (https://github.com/epaultex-svg/OneHealth)"
_TIMEOUT_SECONDS = 5.0

# Nominatim address keys, most-specific first. A reverse lookup may lack `city`
# (rural areas, small municipalities), so fall back through coarser place types.
_CITY_KEYS = ("city", "town", "village", "municipality", "county")


def reverse_geocode_city(lat: float, lng: float) -> str | None:
    """Return the city name for the given coordinates, or None on any failure."""
    try:
        response = httpx.get(
            NOMINATIM_REVERSE_URL,
            params={
                "lat": lat,
                "lon": lng,
                "format": "jsonv2",
                "zoom": 10,
                "addressdetails": 1,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        address = response.json().get("address", {})
    except Exception:
        return None

    for key in _CITY_KEYS:
        value = address.get(key)
        if value:
            return value
    return None
