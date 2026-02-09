#!/usr/bin/env python3
"""Album cache pipeline.

Phase 1: Extract a deduped albums manifest from an Apple Music/iTunes Library.xml
Phase 2: Resolve collection IDs and artwork URLs using iTunes Search API
"""

from __future__ import annotations

import argparse
import csv
import json
import plistlib
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException

SEARCH_ENDPOINT = "https://itunes.apple.com/search"
LOOKUP_ENDPOINT = "https://itunes.apple.com/lookup"


def normalize_text(value: str) -> str:
    """Normalize text for matching and dedupe keys."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = text.lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def strip_editions(value: str) -> str:
    """Strip common edition/version qualifiers from album titles."""
    if not value:
        return ""
    text = value
    text = re.sub(
        r"\s*[\[(].*?(deluxe|expanded|remaster|remastered|edition|anniversary|mono|stereo|bonus|special|version|live).*?[\])]\s*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*-\s*(deluxe|expanded|remaster|remastered|edition|anniversary|mono|stereo|bonus|special|version|live).*",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())


def ensure_out_dir(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)


def _track_sort_key(value: str) -> Tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return (2**31 - 1), value


def extract_albums(library_path: Path, out_dir: Path) -> None:
    """Parse Library.xml and emit albums.json + albums.csv."""
    ensure_out_dir(out_dir)
    with library_path.open("rb") as handle:
        plist = plistlib.load(handle)

    tracks: Dict[str, Dict[str, Any]] = plist.get("Tracks", {})
    total_tracks = len(tracks)

    albums: Dict[str, Dict[str, Any]] = {}
    for track_id in sorted(tracks.keys(), key=_track_sort_key):
        track = tracks[track_id]
        album = track.get("Album")
        if not album:
            continue
        album_artist = track.get("Album Artist") or track.get("Artist")
        if not album_artist:
            continue

        text_key = f"{normalize_text(album_artist)} — {normalize_text(album)}"
        if not text_key.strip(" —"):
            continue

        entry = albums.get(text_key)
        if entry is None:
            entry = {
                "text_key": text_key,
                "album_artist": album_artist,
                "album": album,
                "representative_track": None,
            }
            albums[text_key] = entry

        if entry["representative_track"] is None:
            if all(track.get(field) for field in ("Name", "Artist", "Album", "Total Time")):
                entry["representative_track"] = {
                    "title": track.get("Name"),
                    "artist": track.get("Artist"),
                    "duration_ms": track.get("Total Time"),
                }

    album_list = sorted(
        albums.values(),
        key=lambda item: (normalize_text(item["album_artist"]), normalize_text(item["album"])),
    )

    albums_json_path = out_dir / "albums.json"
    with albums_json_path.open("w", encoding="utf-8") as handle:
        json.dump(album_list, handle, indent=2, ensure_ascii=False)

    albums_csv_path = out_dir / "albums.csv"
    with albums_csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track_title",
                "rep_track_artist",
                "rep_track_duration_ms",
            ],
        )
        writer.writeheader()
        for entry in album_list:
            rep = entry.get("representative_track") or {}
            writer.writerow(
                {
                    "text_key": entry["text_key"],
                    "album_artist": entry["album_artist"],
                    "album": entry["album"],
                    "rep_track_title": rep.get("title", ""),
                    "rep_track_artist": rep.get("artist", ""),
                    "rep_track_duration_ms": rep.get("duration_ms", ""),
                }
            )

    print(f"Total tracks: {total_tracks}")
    print(f"Total unique albums: {len(album_list)}")
    print(f"Wrote: {albums_json_path}")
    print(f"Wrote: {albums_csv_path}")


@dataclass
class RequestState:
    consecutive_429: int = 0


@dataclass
class Candidate:
    score: float
    result: Dict[str, Any]


def derive_artwork_url(url: str, size: int) -> str:
    if not url:
        return ""
    replaced = re.sub(r"/\d+x\d+bb", f"/{size}x{size}bb", url, count=1)
    if replaced == url:
        replaced = re.sub(r"\d+x\d+", f"{size}x{size}", url, count=1)
    return replaced


def _sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _parse_retry_after(response: Response) -> Optional[int]:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def request_json_with_retries(
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    max_retries: int,
    state: RequestState,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Perform a GET request with retries and polite 429 handling."""
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        try:
            if verbose_http:
                print(f"GET {url} params={params}")
            response = session.get(url, params=params, timeout=20)
        except RequestException as exc:
            last_error = f"request error: {exc}"
            backoff = min(2**attempt, 60)
            _sleep_seconds(backoff)
            continue

        status = response.status_code
        if status == 200:
            state.consecutive_429 = 0
            try:
                return response.json(), None
            except ValueError as exc:
                return None, f"invalid json: {exc}"

        if status == 429:
            state.consecutive_429 += 1
            retry_after = _parse_retry_after(response)
            wait_seconds = min(retry_after if retry_after is not None else 2**attempt, 60)
            if verbose_429:
                if retry_after is not None:
                    print(f"429 Retry-After={retry_after}s -> sleeping {wait_seconds}s")
                else:
                    print(f"429 backoff -> sleeping {wait_seconds}s")
            _sleep_seconds(wait_seconds)
            if state.consecutive_429 >= max_429_in_row:
                if verbose_429:
                    print(f"429 cooldown -> sleeping {cooldown_seconds}s")
                _sleep_seconds(cooldown_seconds)
                state.consecutive_429 = 0
            last_error = f"HTTP 429"
            continue

        if status >= 500 or status == 408:
            backoff = min(2**attempt, 60)
            _sleep_seconds(backoff)
            last_error = f"HTTP {status}"
            continue

        return None, f"HTTP {status}"

    return None, last_error or "max retries exceeded"


def score_candidate(
    result: Dict[str, Any],
    rep_track_title: str,
    rep_track_artist: str,
    rep_album: str,
    rep_duration_ms: Optional[int],
) -> float:
    """Score a search result candidate."""
    score = 0.0
    track_name = result.get("trackName", "")
    artist_name = result.get("artistName", "")
    collection_name = result.get("collectionName", "")

    norm_track = normalize_text(track_name)
    norm_rep_track = normalize_text(rep_track_title)
    if norm_track and norm_track == norm_rep_track:
        score += 6.0
    elif norm_rep_track and (norm_rep_track in norm_track or norm_track in norm_rep_track):
        score += 3.0

    norm_artist = normalize_text(artist_name)
    norm_rep_artist = normalize_text(rep_track_artist)
    if norm_artist and norm_artist == norm_rep_artist:
        score += 4.0
    elif norm_rep_artist and (norm_rep_artist in norm_artist or norm_artist in norm_rep_artist):
        score += 2.0

    norm_collection = normalize_text(strip_editions(collection_name))
    norm_rep_album = normalize_text(strip_editions(rep_album))
    if norm_collection and norm_collection == norm_rep_album:
        score += 3.0
    elif norm_rep_album and (norm_rep_album in norm_collection or norm_collection in norm_rep_album):
        score += 1.5

    if rep_duration_ms and result.get("trackTimeMillis"):
        diff = abs(int(result["trackTimeMillis"]) - int(rep_duration_ms))
        if diff <= 2000:
            score += 2.0
        elif diff <= 5000:
            score += 1.0
        elif diff <= 10000:
            score += 0.5

    return score


def load_album_map(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_album_map(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)


def resolve_albums(
    albums_json: Path,
    out_dir: Path,
    country: str,
    sleep_ms: int,
    resume: bool,
    search_limit: int,
    min_score: float,
    ambiguity_gap: float,
    max_429_in_row: int,
    cooldown_seconds: int,
    max_retries: int,
    verbose_429: bool,
    verbose_http: bool,
    lookup: bool,
    progress_every: int,
) -> None:
    """Resolve albums to collection IDs and artwork URLs."""
    ensure_out_dir(out_dir)

    with albums_json.open("r", encoding="utf-8") as handle:
        albums: List[Dict[str, Any]] = json.load(handle)

    album_map_path = out_dir / "album_id_map.json"
    album_id_map: Dict[str, Dict[str, Any]] = load_album_map(album_map_path) if resume else {}

    resolved_rows: List[Dict[str, Any]] = []
    ambiguous_rows: List[Dict[str, Any]] = []
    unresolved_rows: List[Dict[str, Any]] = []

    session = requests.Session()
    state = RequestState()

    processed = 0
    skipped = 0

    for entry in albums:
        text_key = entry.get("text_key", "")
        if not text_key:
            continue
        if resume and text_key in album_id_map:
            skipped += 1
            continue

        processed += 1
        rep = entry.get("representative_track") or {}
        rep_title = rep.get("title")
        rep_artist = rep.get("artist")
        rep_duration = rep.get("duration_ms")
        album_title = entry.get("album", "")

        if not rep_title or not rep_artist:
            unresolved_rows.append(
                {
                    "text_key": text_key,
                    "album_artist": entry.get("album_artist", ""),
                    "album": album_title,
                    "reason": "missing representative track",
                }
            )
            continue

        term = f"{rep_title} {rep_artist}"
        params = {
            "term": term,
            "entity": "song",
            "country": country,
            "limit": search_limit,
        }

        data, error = request_json_with_retries(
            session,
            SEARCH_ENDPOINT,
            params,
            max_retries,
            state,
            max_429_in_row,
            cooldown_seconds,
            verbose_429,
            verbose_http,
        )

        if error or not data:
            unresolved_rows.append(
                {
                    "text_key": text_key,
                    "album_artist": entry.get("album_artist", ""),
                    "album": album_title,
                    "reason": error or "no response",
                }
            )
            _sleep_seconds(sleep_ms / 1000.0)
            continue

        results = data.get("results", [])
        candidates: List[Candidate] = []
        for result in results:
            if not result.get("collectionId") or not result.get("trackName"):
                continue
            score = score_candidate(result, rep_title, rep_artist, album_title, rep_duration)
            candidates.append(Candidate(score=score, result=result))

        if not candidates:
            unresolved_rows.append(
                {
                    "text_key": text_key,
                    "album_artist": entry.get("album_artist", ""),
                    "album": album_title,
                    "reason": "no candidates",
                }
            )
            _sleep_seconds(sleep_ms / 1000.0)
            continue

        candidates.sort(key=lambda item: item.score, reverse=True)
        best = candidates[0]
        second_score = candidates[1].score if len(candidates) > 1 else None

        if best.score < min_score:
            unresolved_rows.append(
                {
                    "text_key": text_key,
                    "album_artist": entry.get("album_artist", ""),
                    "album": album_title,
                    "reason": f"low score ({best.score:.2f})",
                }
            )
            _sleep_seconds(sleep_ms / 1000.0)
            continue

        if second_score is not None and (best.score - second_score) < ambiguity_gap:
            ambiguous_rows.append(
                {
                    "text_key": text_key,
                    "album_artist": entry.get("album_artist", ""),
                    "album": album_title,
                    "best_score": f"{best.score:.2f}",
                    "second_score": f"{second_score:.2f}",
                    "best_collection": best.result.get("collectionName", ""),
                    "best_artist": best.result.get("artistName", ""),
                    "best_track": best.result.get("trackName", ""),
                }
            )
            _sleep_seconds(sleep_ms / 1000.0)
            continue

        collection_id = best.result.get("collectionId")
        artwork_100 = best.result.get("artworkUrl100", "")
        record: Dict[str, Any] = {
            "collectionId": collection_id,
            "collectionName": best.result.get("collectionName", ""),
            "artistName": best.result.get("artistName", ""),
            "artworkUrl100": artwork_100,
            "artworkUrl600": derive_artwork_url(artwork_100, 600),
            "artworkUrl3000": derive_artwork_url(artwork_100, 3000),
        }

        if lookup and collection_id:
            lookup_params = {"id": collection_id, "entity": "album"}
            lookup_data, lookup_error = request_json_with_retries(
                session,
                LOOKUP_ENDPOINT,
                lookup_params,
                max_retries,
                state,
                max_429_in_row,
                cooldown_seconds,
                verbose_429,
                verbose_http,
            )
            if not lookup_error and lookup_data:
                for item in lookup_data.get("results", []):
                    if item.get("wrapperType") == "collection":
                        if item.get("primaryGenreName"):
                            record["primaryGenreName"] = item.get("primaryGenreName")
                        if item.get("releaseDate"):
                            record["releaseDate"] = item.get("releaseDate")
                        break

        album_id_map[text_key] = record
        resolved_rows.append(
            {
                "text_key": text_key,
                "album_artist": entry.get("album_artist", ""),
                "album": album_title,
                "collectionId": record.get("collectionId", ""),
                "collectionName": record.get("collectionName", ""),
                "artistName": record.get("artistName", ""),
                "artworkUrl100": record.get("artworkUrl100", ""),
                "artworkUrl600": record.get("artworkUrl600", ""),
                "artworkUrl3000": record.get("artworkUrl3000", ""),
            }
        )

        if processed % progress_every == 0:
            write_album_map(album_map_path, album_id_map)
            print(
                "Processed "
                f"{processed}/{len(albums)} "
                f"(resolved: {len(resolved_rows)}, ambiguous: {len(ambiguous_rows)}, "
                f"unresolved: {len(unresolved_rows)}, skipped: {skipped})"
            )

        _sleep_seconds(sleep_ms / 1000.0)

    write_album_map(album_map_path, album_id_map)

    resolved_rows_all: List[Dict[str, Any]] = []
    for album in albums:
        album_key = album.get("text_key")
        if not album_key or album_key not in album_id_map:
            continue
        record = album_id_map[album_key]
        resolved_rows_all.append(
            {
                "text_key": album_key,
                "album_artist": album.get("album_artist", ""),
                "album": album.get("album", ""),
                "collectionId": record.get("collectionId", ""),
                "collectionName": record.get("collectionName", ""),
                "artistName": record.get("artistName", ""),
                "artworkUrl100": record.get("artworkUrl100", ""),
                "artworkUrl600": record.get("artworkUrl600", ""),
                "artworkUrl3000": record.get("artworkUrl3000", ""),
            }
        )

    resolved_csv = out_dir / "resolved.csv"
    with resolved_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "collectionId",
                "collectionName",
                "artistName",
                "artworkUrl100",
                "artworkUrl600",
                "artworkUrl3000",
            ],
        )
        writer.writeheader()
        for row in resolved_rows_all:
            writer.writerow(row)

    ambiguous_csv = out_dir / "ambiguous.csv"
    with ambiguous_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "best_score",
                "second_score",
                "best_collection",
                "best_artist",
                "best_track",
            ],
        )
        writer.writeheader()
        for row in ambiguous_rows:
            writer.writerow(row)

    unresolved_csv = out_dir / "unresolved.csv"
    with unresolved_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["text_key", "album_artist", "album", "reason"],
        )
        writer.writeheader()
        for row in unresolved_rows:
            writer.writerow(row)

    print(f"Wrote: {album_map_path}")
    print(f"Wrote: {resolved_csv}")
    print(f"Wrote: {ambiguous_csv}")
    print(f"Wrote: {unresolved_csv}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Album cache pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser("extract", help="Extract album manifest")
    extract_parser.add_argument("--library", required=True, type=Path, help="Path to Library.xml")
    extract_parser.add_argument("--out", type=Path, default=Path("python/out"), help="Output directory")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve album IDs and artwork")
    resolve_parser.add_argument(
        "--albums-json",
        type=Path,
        default=Path("python/out/albums.json"),
        help="Albums JSON from extract phase",
    )
    resolve_parser.add_argument("--out", type=Path, default=Path("python/out"), help="Output directory")
    resolve_parser.add_argument("--country", default="us", help="iTunes country code")
    resolve_parser.add_argument("--sleep-ms", type=int, default=1500, help="Sleep between requests (ms)")
    resolve_parser.add_argument(
        "--resume",
        type=lambda value: value.lower() not in {"false", "0", "no"},
        default=True,
        help="Resume using existing album_id_map.json (default true)",
    )
    resolve_parser.add_argument("--search-limit", type=int, default=25, help="Search results limit")
    resolve_parser.add_argument("--min-score", type=float, default=8.0, help="Minimum score to resolve")
    resolve_parser.add_argument(
        "--ambiguity-gap", type=float, default=0.75, help="Min gap between top scores"
    )
    resolve_parser.add_argument("--max-429-in-row", type=int, default=3, help="Max consecutive 429s")
    resolve_parser.add_argument("--cooldown-seconds", type=int, default=600, help="Cooldown after 429s")
    resolve_parser.add_argument("--max-retries", type=int, default=4, help="Max retries per request")
    resolve_parser.add_argument("--verbose-429", action="store_true", help="Log 429 handling")
    resolve_parser.add_argument("--verbose-http", action="store_true", help="Log HTTP requests")
    resolve_parser.add_argument("--lookup", action="store_true", help="Enable lookup hydration")
    resolve_parser.add_argument(
        "--progress-every", type=int, default=25, help="Print progress + flush map every N"
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "extract":
        extract_albums(args.library, args.out)
        return

    if args.command == "resolve":
        resolve_albums(
            albums_json=args.albums_json,
            out_dir=args.out,
            country=args.country,
            sleep_ms=args.sleep_ms,
            resume=args.resume,
            search_limit=args.search_limit,
            min_score=args.min_score,
            ambiguity_gap=args.ambiguity_gap,
            max_429_in_row=args.max_429_in_row,
            cooldown_seconds=args.cooldown_seconds,
            max_retries=args.max_retries,
            verbose_429=args.verbose_429,
            verbose_http=args.verbose_http,
            lookup=args.lookup,
            progress_every=args.progress_every,
        )
        return


if __name__ == "__main__":
    main()
