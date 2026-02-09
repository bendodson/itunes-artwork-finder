# Python Album Cache Pipeline

This directory contains a resumable CLI pipeline for building a local
album cache from an Apple Music / iTunes `Library.xml` export.

## Setup

Create a virtual environment and install requirements:

```bash
cd python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Phase 1: Extract Albums

Parse `Library.xml` and generate a deduped albums manifest (JSON + CSV).

```bash
# from repo root
python python/album_cache.py extract --library /path/to/Library.xml
```

Outputs (default):
- `python/out/albums.json`
- `python/out/albums.csv`

## Phase 2: Resolve Collection IDs + Artwork

Resolve each album using iTunes Search API calls with deterministic tie-breakers,
optional fallback strategies, and local overrides.

```bash
# from repo root
python python/album_cache.py resolve --country us --sleep-ms 1500 --resume
```

Useful flags:
- `--overrides out/overrides.json` (default)
- `--album-fallback` / `--no-album-fallback`
- `--fallback-only-on-empty`
- `--prefer-explicit` / `--no-prefer-explicit`
- `--prefer-non-deluxe` / `--no-prefer-non-deluxe`
- `--prefer-original` / `--no-prefer-original`
- `--lookup` (optional hydration)

Fallback order when needed:
1. Primary song search using representative track + artist
2. Album fallback search (`entity=album`) using album + album artist
3. Final song search using album + album artist

Outputs (default):
- `python/out/album_id_map.json`
- `python/out/resolved.csv`
- `python/out/ambiguous.csv`
- `python/out/unresolved.csv`

`resolved.csv`, `ambiguous.csv`, and `unresolved.csv` include these key fields:
- `resolution_path` (`song_primary`, `album_fallback`, `song_album_term`, `override`)
- `best_collectionId`, `best_collectionName`, `best_artistName`, `best_score`
- artwork URLs for resolved rows (`artworkUrl100`, `artworkUrl600`, `artworkUrl3000`)
- `error` for unresolved rows (and optional diagnostics for resolved rows)

## Overrides

Use local overrides to force known problematic albums:

`out/overrides.json` format:

```json
{
  "text_key": 1234567890
}
```

- Keys are `text_key` values from extracted albums.
- Values are numeric `collectionId` values.
- If an override is present, resolve uses it before search calls.
- `out/overrides.json` is gitignored.

## Review Workflow (Ambiguous/Unresolved)

1. Run resolve:

```bash
python python/album_cache.py resolve --resume
```

2. Review ambiguous/unresolved rows and write overrides interactively:

```bash
python python/album_cache.py review --mode both --overrides out/overrides.json
```

3. Re-run resolve to apply new overrides:

```bash
python python/album_cache.py resolve --resume --overrides out/overrides.json
```

## Polite API Usage

This pipeline is designed to be resumable and polite:
- Sleeps between albums (default 1500ms)
- Handles HTTP 429 with Retry-After or exponential backoff
- Uses cooldown after bursts of 429 responses
- Caps retries to avoid request storms
- Shares one request state/rate-limiter path for all search/lookup calls
