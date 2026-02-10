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

## Phase 3: Download Source Artwork

Download square source artwork from the already-resolved Apple artwork CDN URLs in
`album_id_map.json` (no iTunes Search/Lookup calls in this step).

```bash
python python/album_cache.py download-art \
  --map-json python/out/album_id_map.json \
  --out-dir out/source_art \
  --prefer-size hires \
  --resume
```

Defaults for `download-art`:
- `--map-json out/album_id_map.json`
- `--out-dir out/source_art`
- `--prefer-size hires`
- `--concurrency 2`
- `--timeout-s 30`
- `--retries 3`
- `--sleep-ms 150`
- `--resume` enabled
- `--format keep`
- `--manifest out/source_art/manifest.json`

Outputs:
- Source artwork files in `out/source_art/`
- Download manifest in `out/source_art/manifest.json`

Hires behavior:
- For `--prefer-size hires`, the downloader transforms size tokens like
  `100x100bb` to `100000x100000-999` (same pattern used by the original
  `itunes-artwork-finder` project) before downloading from Apple's artwork CDN.

## Phase 4: Generate Widescreen Frame TV Artwork (16:9)

Generate final 3840x2160 widescreen PNGs through a two-stage pipeline:
- Stage 1 (API edit): build a 1536x1024 PNG canvas, place a centered album square,
  and run OpenAI image edits only on the surrounding area.
- Stage 2 (final output): upscale the edited 1536x1024 result to 3840x2160 with
  LANCZOS, then re-paste the original album at the exact final center position.

Alternative route for policy-sensitive runs:
- `--background-mode reference-no-mask`: uploads only a reference canvas (no mask)
  and asks the model to generate an original 16:9 background matching palette/mood.
  The script then pastes the original album back into the final center.

```bash
python python/album_cache.py generate-widescreen \
  --source-art-dir out/source_art \
  --out-dir out/widescreen \
  --map-json out/album_id_map.json \
  --target-width 3840 \
  --target-height 2160 \
  --album-px 1536 \
  --model gpt-image-1.5 \
  --background-mode reference-no-mask \
  --style-profile neutral \
  --resume \
  --sleep-ms 2500
```

GPT-Image-1.5 editing behavior:
- Phase 4 uses OpenAI's Images edit API at `/v1/images/edits` (or `OPENAI_IMAGES_EDITS_URL` override).
- The request uploads:
  - base canvas image bytes (`image[]` for GPT image models)
  - mask bytes (`mask`)
  - prompt text (`prompt`)
  - explicit model (`model=gpt-image-1.5` when requested)
  - optional size for supported GPT image sizes (`1024x1024`, `1536x1024`, `1024x1536`)
- If the runtime account/region rollout does not yet allow GPT-Image-1.5 for edits and the API returns a model-support error (for example `invalid model`), the script automatically falls back to `dall-e-2` so runs can continue.
- Select GPT-Image-1.5 explicitly with `--model gpt-image-1.5`.
- The edit mask is an RGBA PNG and must match image dimensions exactly. Transparent
  pixels (`alpha=0`) are editable background; opaque pixels (`alpha=255`) protect
  the centered album region.
- In `reference-no-mask` mode, no mask is uploaded (`image[]` only).

Defaults for `generate-widescreen`:
- `--source-art-dir out/source_art`
- `--out-dir out/widescreen`
- `--map-json out/album_id_map.json`
- `--target-width 3840`
- `--target-height 2160`
- `--album-px 1536`
- `--style-profile neutral`
- `--resume` enabled (skip if existing output > 100KB)
- `--sleep-ms 2500`

Other flags:
- `--seed` (optional)
- `--debug-save-canvas` (saves debug canvas/mask per album)

Environment:
- Set `OPENAI_API_KEY` before running.
- Optional endpoint override: `OPENAI_IMAGES_EDITS_URL` (must point to an Images edits endpoint compatible with GPT image models for masked edits).

Outputs:
- Widescreen files in `out/widescreen/` named `<collectionId-or-contentId>__3840x2160.png`
- If both IDs are missing, fallback naming is `unknown__<sha1(text_key)[:12]>__3840x2160.png`
- Run manifest in `out/widescreen/manifest.json`
- Optional lookup index in `out/widescreen/index.json`

## Polite API Usage

This pipeline is designed to be resumable and polite:
- Sleeps between albums (default 1500ms)
- Handles HTTP 429 with Retry-After or exponential backoff
- Uses cooldown after bursts of 429 responses
- Caps retries to avoid request storms
- Shares one request state/rate-limiter path for all search/lookup calls
