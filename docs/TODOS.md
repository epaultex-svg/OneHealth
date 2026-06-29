# TODOS

## Bulk backfill of `city` for existing location rows

- **What:** A one-time, throttled (~1 req/s for Nominatim) script that reads every
  `users` row whose `location` has `{lat,lng}` but no `city`, reverse-geocodes it, and
  writes `city` back.
- **Why:** Removes the one-time ~1-5s latency an existing user hits on their first
  appointment draft or info retrieval after deploy (the lazy read-fallback geocode).
- **Pros:** Clean read path; no per-user first-hit latency.
- **Cons:** Extra script + a slow throttled batch + an ops step to run it. The inline
  read-fallback (`tools.resolve_location_city`) already self-heals, so value is marginal.
- **Context:** We display saved location as a city instead of lat/lng. Geocoding happens
  at write time (`tools.store_info`) and lazily on read for old rows
  (`tools.resolve_location_city`, which geocodes once then backfills). This script would
  just front-load that backfill for all existing rows at once.
- **Depends on:** `geocoding.reverse_geocode_city`, `tools.resolve_location_city` (shipped).
- **Priority:** Low.
