# Python Album Cache Pipeline

This directory contains a two-phase Python CLI pipeline for building a local
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

Resolve each album with **one iTunes Search request per album** (no lookup by
default) and write resolved/ambiguous/unresolved outputs.

```bash
# from repo root
python python/album_cache.py resolve --country us --sleep-ms 1500
```

Optional lookup hydration (use sparingly):

```bash
python python/album_cache.py resolve --lookup
```

Outputs (default):
- `python/out/album_id_map.json`
- `python/out/resolved.csv`
- `python/out/ambiguous.csv`
- `python/out/unresolved.csv`

## Polite API Usage

This pipeline is designed to be resumable and polite:
- Sleeps between requests (default 1500ms)
- Handles HTTP 429 with Retry-After or exponential backoff
- Avoids lookup calls unless explicitly enabled
- Caps retries and cooldowns to prevent request storms

## Example Commands

```bash
# Phase 1
python python/album_cache.py extract --library ~/Music/iTunes/Library.xml --out python/out

# Phase 2
python python/album_cache.py resolve --albums-json python/out/albums.json --sleep-ms 1500

# Resume (default) with existing album_id_map.json
python python/album_cache.py resolve --resume
```
