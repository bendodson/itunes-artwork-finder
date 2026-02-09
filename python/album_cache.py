#!/usr/bin/env python3
"""Album cache pipeline.

Phase 1: Extract a deduped albums manifest from an Apple Music/iTunes Library.xml
Phase 2: Resolve collection IDs and artwork URLs using iTunes Search API
Phase 3: Review ambiguous/unresolved rows and create local overrides
"""

from __future__ import annotations

import argparse
import csv
import json
import plistlib
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException

SEARCH_ENDPOINT = "https://itunes.apple.com/search"
LOOKUP_ENDPOINT = "https://itunes.apple.com/lookup"

DELUXE_KEYWORDS = (
    "deluxe",
    "expanded",
    "bonus",
    "special",
    "edition",
)
REMASTER_KEYWORDS = ("remaster", "remastered")
ANNIVERSARY_KEYWORDS = ("anniversary",)
ORIGINAL_AVOID_KEYWORDS = REMASTER_KEYWORDS + ANNIVERSARY_KEYWORDS + ("reissue",)
EXPLICIT_KEYWORDS = ("explicit",)
CLEAN_KEYWORDS = ("clean",)


@dataclass
class RequestState:
    consecutive_429: int = 0


@dataclass
class Candidate:
    score: float
    result: Dict[str, Any]


def normalize_text(value: str) -> str:
    """Normalize text for matching and dedupe keys."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = text.lower().strip()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def has_any_keyword(value: str, keywords: Tuple[str, ...]) -> bool:
    if not value:
        return False
    lower = value.lower()
    for keyword in keywords:
        if re.search(rf"\\b{re.escape(keyword)}\\b", lower):
            return True
    return False


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


def _sleep_seconds(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def derive_artwork_url(url: str, size: int) -> str:
    if not url:
        return ""
    replaced = re.sub(r"/\d+x\d+bb", f"/{size}x{size}bb", url, count=1)
    if replaced == url:
        replaced = re.sub(r"\d+x\d+", f"{size}x{size}", url, count=1)
    return replaced


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
            last_error = "HTTP 429"
            continue

        if status >= 500 or status == 408:
            backoff = min(2**attempt, 60)
            _sleep_seconds(backoff)
            last_error = f"HTTP {status}"
            continue

        return None, f"HTTP {status}"

    return None, last_error or "max retries exceeded"


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


def score_song_candidate(
    result: Dict[str, Any],
    rep_track_title: str,
    rep_track_artist: str,
    rep_album: str,
    rep_duration_ms: Optional[int],
) -> float:
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


def score_album_candidate(result: Dict[str, Any], album_title: str, album_artist: str) -> float:
    score = 0.0
    collection_name = result.get("collectionName", "")
    artist_name = result.get("artistName", "")

    lib_album_full = normalize_text(album_title)
    lib_album_core = normalize_text(strip_editions(album_title))
    cand_album_full = normalize_text(collection_name)
    cand_album_core = normalize_text(strip_editions(collection_name))

    if lib_album_full and cand_album_full == lib_album_full:
        score += 6.0
    elif lib_album_full and (lib_album_full in cand_album_full or cand_album_full in lib_album_full):
        score += 3.0

    if lib_album_core and cand_album_core == lib_album_core:
        score += 3.0
    elif lib_album_core and (lib_album_core in cand_album_core or cand_album_core in lib_album_core):
        score += 1.5

    lib_artist = normalize_text(album_artist)
    cand_artist = normalize_text(artist_name)
    if lib_artist and cand_artist == lib_artist:
        score += 3.0
    elif lib_artist and (lib_artist in cand_artist or cand_artist in lib_artist):
        score += 1.5

    return score


def _candidate_sort_key(item: Candidate) -> Tuple[float, int, str]:
    collection_id = _safe_int(item.result.get("collectionId")) or 2**31 - 1
    collection_name = normalize_text(item.result.get("collectionName", ""))
    return (-item.score, collection_id, collection_name)


def _duration_distance(candidate: Candidate, rep_duration_ms: Optional[int]) -> Optional[int]:
    if not rep_duration_ms:
        return None
    track_time = _safe_int(candidate.result.get("trackTimeMillis"))
    if not track_time:
        return None
    return abs(track_time - rep_duration_ms)


def _explicit_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, EXPLICIT_KEYWORDS)


def _edition_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, DELUXE_KEYWORDS)


def _original_avoid_hint(candidate: Candidate) -> bool:
    blob = f"{candidate.result.get('collectionName', '')} {candidate.result.get('trackName', '')}"
    return has_any_keyword(blob, ORIGINAL_AVOID_KEYWORDS)


def choose_with_tiebreakers(
    best: Candidate,
    second: Candidate,
    album_title: str,
    rep_duration_ms: Optional[int],
    prefer_explicit: bool,
    prefer_non_deluxe: bool,
    prefer_original: bool,
) -> Optional[Candidate]:
    lib_album_norm = normalize_text(album_title)
    lib_album_core = normalize_text(strip_editions(album_title))

    def exact_album_match(candidate: Candidate) -> bool:
        return normalize_text(candidate.result.get("collectionName", "")) == lib_album_norm

    def contains_album_core(candidate: Candidate) -> bool:
        collection_norm = normalize_text(candidate.result.get("collectionName", ""))
        return bool(lib_album_core and lib_album_core in collection_norm)

    checks = [
        exact_album_match,
        contains_album_core,
    ]

    for check in checks:
        left = check(best)
        right = check(second)
        if left != right:
            return best if left else second

    if prefer_explicit:
        library_wants_explicit = has_any_keyword(album_title, EXPLICIT_KEYWORDS)
        library_mentions_clean = has_any_keyword(album_title, CLEAN_KEYWORDS)
        best_explicit = _explicit_hint(best)
        second_explicit = _explicit_hint(second)
        if best_explicit != second_explicit and (library_wants_explicit or not library_mentions_clean):
            return best if best_explicit else second

    if prefer_non_deluxe and not has_any_keyword(album_title, DELUXE_KEYWORDS):
        best_deluxe = _edition_hint(best)
        second_deluxe = _edition_hint(second)
        if best_deluxe != second_deluxe:
            return second if best_deluxe else best

    if prefer_original and not has_any_keyword(album_title, ORIGINAL_AVOID_KEYWORDS):
        best_avoid = _original_avoid_hint(best)
        second_avoid = _original_avoid_hint(second)
        if best_avoid != second_avoid:
            return second if best_avoid else best

    best_diff = _duration_distance(best, rep_duration_ms)
    second_diff = _duration_distance(second, rep_duration_ms)
    if best_diff is not None and second_diff is not None and best_diff != second_diff:
        return best if best_diff < second_diff else second

    return None


def search_candidates(
    session: requests.Session,
    state: RequestState,
    term: str,
    entity: str,
    country: str,
    search_limit: int,
    max_retries: int,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    params = {
        "term": term,
        "entity": entity,
        "country": country,
        "limit": search_limit,
    }
    data, error = request_json_with_retries(
        session=session,
        url=SEARCH_ENDPOINT,
        params=params,
        max_retries=max_retries,
        state=state,
        max_429_in_row=max_429_in_row,
        cooldown_seconds=cooldown_seconds,
        verbose_429=verbose_429,
        verbose_http=verbose_http,
    )
    if error or not data:
        return [], error or "no response"
    return data.get("results", []), None


def lookup_collection_once(
    session: requests.Session,
    state: RequestState,
    collection_id: int,
    max_retries: int,
    max_429_in_row: int,
    cooldown_seconds: int,
    verbose_429: bool,
    verbose_http: bool,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    params = {"id": collection_id, "entity": "album"}
    data, error = request_json_with_retries(
        session=session,
        url=LOOKUP_ENDPOINT,
        params=params,
        max_retries=max_retries,
        state=state,
        max_429_in_row=max_429_in_row,
        cooldown_seconds=cooldown_seconds,
        verbose_429=verbose_429,
        verbose_http=verbose_http,
    )
    if error or not data:
        return None, error or "no response"
    for item in data.get("results", []):
        if item.get("wrapperType") == "collection":
            return item, None
    return None, "collection not found"


def load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        return {}
    return data


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    ensure_out_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    temp_path.replace(path)


def load_album_map(path: Path) -> Dict[str, Dict[str, Any]]:
    data = load_json_dict(path)
    output: Dict[str, Dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, dict):
            output[key] = value
    return output


def write_album_map(path: Path, data: Dict[str, Dict[str, Any]]) -> None:
    write_json_atomic(path, data)


def load_overrides(path: Path) -> Dict[str, int]:
    data = load_json_dict(path)
    overrides: Dict[str, int] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        num = _safe_int(value)
        if num is not None:
            overrides[key] = num
    return overrides


def write_overrides(path: Path, overrides: Dict[str, int]) -> None:
    normalized = {key: int(value) for key, value in sorted(overrides.items())}
    write_json_atomic(path, normalized)


def _candidate_row_fields(candidate: Optional[Candidate]) -> Dict[str, Any]:
    if not candidate:
        return {
            "best_collectionId": "",
            "best_collectionName": "",
            "best_artistName": "",
            "best_score": "",
        }
    return {
        "best_collectionId": candidate.result.get("collectionId", ""),
        "best_collectionName": candidate.result.get("collectionName", ""),
        "best_artistName": candidate.result.get("artistName", ""),
        "best_score": f"{candidate.score:.2f}",
    }


def _build_record_from_result(
    result: Dict[str, Any],
    score: Optional[float],
    resolution_path: str,
) -> Dict[str, Any]:
    artwork_100 = result.get("artworkUrl100", "") or ""
    return {
        "collectionId": result.get("collectionId", ""),
        "collectionName": result.get("collectionName", ""),
        "artistName": result.get("artistName", ""),
        "artworkUrl100": artwork_100,
        "artworkUrl600": derive_artwork_url(artwork_100, 600),
        "artworkUrl3000": derive_artwork_url(artwork_100, 3000),
        "resolution_path": resolution_path,
        "best_collectionId": result.get("collectionId", ""),
        "best_collectionName": result.get("collectionName", ""),
        "best_artistName": result.get("artistName", ""),
        "best_score": f"{score:.2f}" if score is not None else "",
    }


def _base_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    rep = entry.get("representative_track") or {}
    return {
        "text_key": entry.get("text_key", ""),
        "album_artist": entry.get("album_artist", ""),
        "album": entry.get("album", ""),
        "rep_track": rep.get("title", ""),
        "rep_track_artist": rep.get("artist", ""),
        "rep_duration_ms": rep.get("duration_ms", ""),
    }


def resolve_albums(
    albums_json: Path,
    out_dir: Path,
    country: str,
    sleep_ms: int,
    resume: bool,
    search_limit: int,
    min_score: float,
    min_score_album: float,
    ambiguity_gap: float,
    max_429_in_row: int,
    cooldown_seconds: int,
    max_retries: int,
    verbose_429: bool,
    verbose_http: bool,
    lookup: bool,
    progress_every: int,
    overrides_path: Path,
    write_overrides_flag: bool,
    album_fallback: bool,
    fallback_only_on_empty: bool,
    prefer_explicit: bool,
    prefer_non_deluxe: bool,
    prefer_original: bool,
) -> None:
    ensure_out_dir(out_dir)

    with albums_json.open("r", encoding="utf-8") as handle:
        albums: List[Dict[str, Any]] = json.load(handle)

    album_map_path = out_dir / "album_id_map.json"
    album_id_map: Dict[str, Dict[str, Any]] = load_album_map(album_map_path) if resume else {}
    overrides = load_overrides(overrides_path)

    resolved_rows_run: List[Dict[str, Any]] = []
    ambiguous_rows: List[Dict[str, Any]] = []
    unresolved_rows: List[Dict[str, Any]] = []

    session = requests.Session()
    state = RequestState()

    processed = 0
    skipped = 0
    overrides_updates = 0

    for entry in albums:
        text_key = entry.get("text_key", "")
        if not text_key:
            continue
        override_id = overrides.get(text_key)
        if (
            resume
            and text_key in album_id_map
            and (
                override_id is None
                or _safe_int(album_id_map[text_key].get("collectionId")) == override_id
            )
        ):
            skipped += 1
            continue

        processed += 1
        row_base = _base_row(entry)
        album_title = row_base["album"]
        album_artist = row_base["album_artist"]
        rep_title = row_base["rep_track"]
        rep_artist = row_base["rep_track_artist"]
        rep_duration_ms = _safe_int(row_base["rep_duration_ms"])

        resolution_record: Optional[Dict[str, Any]] = None
        unresolved_error = ""
        best_primary: Optional[Candidate] = None
        second_primary: Optional[Candidate] = None
        ambiguous_resolution_path = "song_primary"

        if override_id is not None:
            existing = album_id_map.get(text_key, {}) if resume else {}
            resolution_record = {
                "collectionId": override_id,
                "collectionName": existing.get("collectionName", ""),
                "artistName": existing.get("artistName", ""),
                "artworkUrl100": existing.get("artworkUrl100", ""),
                "artworkUrl600": existing.get("artworkUrl600", ""),
                "artworkUrl3000": existing.get("artworkUrl3000", ""),
                "resolution_path": "override",
                "best_collectionId": override_id,
                "best_collectionName": existing.get("collectionName", ""),
                "best_artistName": existing.get("artistName", ""),
                "best_score": existing.get("best_score", ""),
            }

            if not resolution_record.get("artworkUrl100"):
                lookup_item, lookup_error = lookup_collection_once(
                    session=session,
                    state=state,
                    collection_id=override_id,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if lookup_item:
                    artwork_100 = lookup_item.get("artworkUrl100", "") or ""
                    resolution_record["collectionName"] = lookup_item.get("collectionName", "")
                    resolution_record["artistName"] = lookup_item.get("artistName", "")
                    resolution_record["best_collectionName"] = lookup_item.get("collectionName", "")
                    resolution_record["best_artistName"] = lookup_item.get("artistName", "")
                    resolution_record["artworkUrl100"] = artwork_100
                    resolution_record["artworkUrl600"] = derive_artwork_url(artwork_100, 600)
                    resolution_record["artworkUrl3000"] = derive_artwork_url(artwork_100, 3000)
                elif lookup_error:
                    unresolved_error = f"override artwork lookup failed: {lookup_error}"

            if write_overrides_flag:
                # Optional mode for future extension: preserve deterministic, local-only overrides.
                write_overrides(overrides_path, overrides)

        elif not rep_title or not rep_artist:
            unresolved_error = "missing representative track"

        else:
            primary_results, primary_error = search_candidates(
                session=session,
                state=state,
                term=f"{rep_title} {rep_artist}",
                entity="song",
                country=country,
                search_limit=search_limit,
                max_retries=max_retries,
                max_429_in_row=max_429_in_row,
                cooldown_seconds=cooldown_seconds,
                verbose_429=verbose_429,
                verbose_http=verbose_http,
            )
            if primary_error:
                unresolved_error = f"song_primary search failed: {primary_error}"

            primary_candidates: List[Candidate] = []
            for result in primary_results:
                if not result.get("collectionId") or not result.get("trackName"):
                    continue
                score = score_song_candidate(result, rep_title, rep_artist, album_title, rep_duration_ms)
                primary_candidates.append(Candidate(score=score, result=result))
            primary_candidates.sort(key=_candidate_sort_key)

            if primary_candidates:
                best_primary = primary_candidates[0]
                second_primary = primary_candidates[1] if len(primary_candidates) > 1 else None

            if best_primary and best_primary.score >= min_score:
                if second_primary and (best_primary.score - second_primary.score) < ambiguity_gap:
                    tie_winner = choose_with_tiebreakers(
                        best=best_primary,
                        second=second_primary,
                        album_title=album_title,
                        rep_duration_ms=rep_duration_ms,
                        prefer_explicit=prefer_explicit,
                        prefer_non_deluxe=prefer_non_deluxe,
                        prefer_original=prefer_original,
                    )
                    if tie_winner:
                        resolution_record = _build_record_from_result(
                            tie_winner.result,
                            tie_winner.score,
                            "song_primary",
                        )
                else:
                    resolution_record = _build_record_from_result(
                        best_primary.result,
                        best_primary.score,
                        "song_primary",
                    )

            if not resolution_record and album_fallback:
                should_run_album_fallback = False
                if not primary_candidates:
                    should_run_album_fallback = True
                elif not fallback_only_on_empty and best_primary and best_primary.score < min_score:
                    should_run_album_fallback = True

                if should_run_album_fallback:
                    album_results, album_error = search_candidates(
                        session=session,
                        state=state,
                        term=f"{album_title} {album_artist}",
                        entity="album",
                        country=country,
                        search_limit=search_limit,
                        max_retries=max_retries,
                        max_429_in_row=max_429_in_row,
                        cooldown_seconds=cooldown_seconds,
                        verbose_429=verbose_429,
                        verbose_http=verbose_http,
                    )
                    if album_error:
                        unresolved_error = (
                            f"{unresolved_error}; " if unresolved_error else ""
                        ) + f"album_fallback search failed: {album_error}"

                    album_candidates: List[Candidate] = []
                    for result in album_results:
                        if not result.get("collectionId"):
                            continue
                        score = score_album_candidate(result, album_title, album_artist)
                        album_candidates.append(Candidate(score=score, result=result))
                    album_candidates.sort(key=_candidate_sort_key)

                    if album_candidates and album_candidates[0].score >= min_score_album:
                        winner = album_candidates[0]
                        resolution_record = _build_record_from_result(
                            winner.result,
                            winner.score,
                            "album_fallback",
                        )

            if not resolution_record:
                song_album_results, song_album_error = search_candidates(
                    session=session,
                    state=state,
                    term=f"{album_title} {album_artist}",
                    entity="song",
                    country=country,
                    search_limit=search_limit,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if song_album_error:
                    unresolved_error = (
                        f"{unresolved_error}; " if unresolved_error else ""
                    ) + f"song_album_term search failed: {song_album_error}"

                song_album_candidates: List[Candidate] = []
                for result in song_album_results:
                    if not result.get("collectionId") or not result.get("trackName"):
                        continue
                    score = score_song_candidate(result, rep_title, rep_artist, album_title, rep_duration_ms)
                    song_album_candidates.append(Candidate(score=score, result=result))
                song_album_candidates.sort(key=_candidate_sort_key)

                if song_album_candidates and song_album_candidates[0].score >= min_score:
                    winner = song_album_candidates[0]
                    runner_up = song_album_candidates[1] if len(song_album_candidates) > 1 else None
                    if runner_up and (winner.score - runner_up.score) < ambiguity_gap:
                        tie_winner = choose_with_tiebreakers(
                            best=winner,
                            second=runner_up,
                            album_title=album_title,
                            rep_duration_ms=rep_duration_ms,
                            prefer_explicit=prefer_explicit,
                            prefer_non_deluxe=prefer_non_deluxe,
                            prefer_original=prefer_original,
                        )
                        if tie_winner:
                            resolution_record = _build_record_from_result(
                                tie_winner.result,
                                tie_winner.score,
                                "song_album_term",
                            )
                        else:
                            best_primary = winner
                            second_primary = runner_up
                            ambiguous_resolution_path = "song_album_term"
                    else:
                        resolution_record = _build_record_from_result(
                            winner.result,
                            winner.score,
                            "song_album_term",
                        )

        if resolution_record:
            collection_id = _safe_int(resolution_record.get("collectionId"))
            if lookup and collection_id:
                lookup_item, lookup_error = lookup_collection_once(
                    session=session,
                    state=state,
                    collection_id=collection_id,
                    max_retries=max_retries,
                    max_429_in_row=max_429_in_row,
                    cooldown_seconds=cooldown_seconds,
                    verbose_429=verbose_429,
                    verbose_http=verbose_http,
                )
                if lookup_item:
                    if lookup_item.get("primaryGenreName"):
                        resolution_record["primaryGenreName"] = lookup_item.get("primaryGenreName")
                    if lookup_item.get("releaseDate"):
                        resolution_record["releaseDate"] = lookup_item.get("releaseDate")
                    if not resolution_record.get("collectionName"):
                        resolution_record["collectionName"] = lookup_item.get("collectionName", "")
                    if not resolution_record.get("artistName"):
                        resolution_record["artistName"] = lookup_item.get("artistName", "")
                elif lookup_error and not unresolved_error:
                    unresolved_error = f"lookup failed: {lookup_error}"

            if unresolved_error:
                resolution_record["error"] = unresolved_error
            album_id_map[text_key] = resolution_record
            resolved_rows_run.append(
                {
                    **row_base,
                    "resolution_path": resolution_record.get("resolution_path", ""),
                    "best_collectionId": resolution_record.get("best_collectionId", ""),
                    "best_collectionName": resolution_record.get("best_collectionName", ""),
                    "best_artistName": resolution_record.get("best_artistName", ""),
                    "best_score": resolution_record.get("best_score", ""),
                    "collectionId": resolution_record.get("collectionId", ""),
                    "collectionName": resolution_record.get("collectionName", ""),
                    "artistName": resolution_record.get("artistName", ""),
                    "artworkUrl100": resolution_record.get("artworkUrl100", ""),
                    "artworkUrl600": resolution_record.get("artworkUrl600", ""),
                    "artworkUrl3000": resolution_record.get("artworkUrl3000", ""),
                    "error": resolution_record.get("error", ""),
                }
            )
        else:
            ambiguous = False
            if best_primary and second_primary:
                if best_primary.score >= min_score and (best_primary.score - second_primary.score) < ambiguity_gap:
                    ambiguous = True

            if ambiguous:
                ambiguous_rows.append(
                    {
                        **row_base,
                        "resolution_path": ambiguous_resolution_path,
                        "best_collectionId": best_primary.result.get("collectionId", ""),
                        "best_collectionName": best_primary.result.get("collectionName", ""),
                        "best_artistName": best_primary.result.get("artistName", ""),
                        "best_score": f"{best_primary.score:.2f}",
                        "second_collectionId": second_primary.result.get("collectionId", ""),
                        "second_collectionName": second_primary.result.get("collectionName", ""),
                        "second_artistName": second_primary.result.get("artistName", ""),
                        "second_score": f"{second_primary.score:.2f}",
                        "error": unresolved_error,
                    }
                )
            else:
                unresolved_rows.append(
                    {
                        **row_base,
                        "resolution_path": ambiguous_resolution_path if best_primary else "",
                        **_candidate_row_fields(best_primary),
                        "error": unresolved_error or "no winner",
                    }
                )

            if write_overrides_flag:
                url_value = entry.get("override_url")
                parsed = parse_collection_id_from_text(str(url_value)) if url_value else None
                if parsed is not None:
                    overrides[text_key] = parsed
                    overrides_updates += 1

        if processed % progress_every == 0:
            write_album_map(album_map_path, album_id_map)
            print(
                "Processed "
                f"{processed}/{len(albums)} "
                f"(resolved this run: {len(resolved_rows_run)}, ambiguous: {len(ambiguous_rows)}, "
                f"unresolved: {len(unresolved_rows)}, skipped: {skipped})"
            )

        _sleep_seconds(sleep_ms / 1000.0)

    write_album_map(album_map_path, album_id_map)
    if write_overrides_flag and overrides_updates:
        write_overrides(overrides_path, overrides)

    resolved_rows_all: List[Dict[str, Any]] = []
    for album in albums:
        album_key = album.get("text_key")
        if not album_key or album_key not in album_id_map:
            continue
        row_base = _base_row(album)
        record = album_id_map[album_key]
        resolved_rows_all.append(
            {
                **row_base,
                "resolution_path": record.get("resolution_path", ""),
                "best_collectionId": record.get("best_collectionId", record.get("collectionId", "")),
                "best_collectionName": record.get("best_collectionName", record.get("collectionName", "")),
                "best_artistName": record.get("best_artistName", record.get("artistName", "")),
                "best_score": record.get("best_score", ""),
                "collectionId": record.get("collectionId", ""),
                "collectionName": record.get("collectionName", ""),
                "artistName": record.get("artistName", ""),
                "artworkUrl100": record.get("artworkUrl100", ""),
                "artworkUrl600": record.get("artworkUrl600", ""),
                "artworkUrl3000": record.get("artworkUrl3000", ""),
                "error": record.get("error", ""),
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
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "collectionId",
                "collectionName",
                "artistName",
                "artworkUrl100",
                "artworkUrl600",
                "artworkUrl3000",
                "error",
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
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "second_collectionId",
                "second_collectionName",
                "second_artistName",
                "second_score",
                "error",
            ],
        )
        writer.writeheader()
        for row in ambiguous_rows:
            writer.writerow(row)

    unresolved_csv = out_dir / "unresolved.csv"
    with unresolved_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "text_key",
                "album_artist",
                "album",
                "rep_track",
                "rep_track_artist",
                "rep_duration_ms",
                "resolution_path",
                "best_collectionId",
                "best_collectionName",
                "best_artistName",
                "best_score",
                "error",
            ],
        )
        writer.writeheader()
        for row in unresolved_rows:
            writer.writerow(row)

    print(f"Wrote: {album_map_path}")
    print(f"Wrote: {resolved_csv}")
    print(f"Wrote: {ambiguous_csv}")
    print(f"Wrote: {unresolved_csv}")


def parse_collection_id_from_text(value: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None

    direct = _safe_int(text)
    if direct is not None:
        return direct

    match = re.search(r"/id(\d+)(?:\b|[/?#])", text)
    if match:
        return int(match.group(1))

    match = re.search(r"\b(\d{6,})\b", text)
    if match:
        return int(match.group(1))

    return None


def _load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def _prompt_choice(prompt: str, allowed: Tuple[str, ...]) -> str:
    while True:
        try:
            value = input(prompt).strip().lower()
        except EOFError:
            return "quit"
        if value in allowed:
            return value
        print(f"Invalid choice. Allowed: {', '.join(allowed)}")


def review_rows(
    ambiguous_csv: Path,
    unresolved_csv: Path,
    overrides_path: Path,
    mode: str,
) -> None:
    overrides = load_overrides(overrides_path)
    ensure_out_dir(overrides_path.parent)

    ambiguous_rows = _load_csv_rows(ambiguous_csv) if mode in {"ambiguous", "both"} else []
    unresolved_rows = _load_csv_rows(unresolved_csv) if mode in {"unresolved", "both"} else []

    print(f"Loaded overrides: {len(overrides)} from {overrides_path}")

    for row in ambiguous_rows:
        text_key = row.get("text_key", "")
        print("\n--- Ambiguous ---")
        print(f"text_key: {text_key}")
        print(f"album_artist: {row.get('album_artist', '')}")
        print(f"album: {row.get('album', '')}")
        print(f"rep_track: {row.get('rep_track', '')}")
        print(
            "1) "
            f"{row.get('best_collectionId', '')} | {row.get('best_collectionName', '')} | "
            f"{row.get('best_artistName', '')} | score={row.get('best_score', '')}"
        )
        print(
            "2) "
            f"{row.get('second_collectionId', '')} | {row.get('second_collectionName', '')} | "
            f"{row.get('second_artistName', '')} | score={row.get('second_score', '')}"
        )
        choice = _prompt_choice("Choose 1/2/skip/quit: ", ("1", "2", "skip", "quit"))
        if choice == "quit":
            break
        if choice == "skip":
            continue

        selected_id = row.get("best_collectionId", "") if choice == "1" else row.get("second_collectionId", "")
        collection_id = _safe_int(selected_id)
        if collection_id is None:
            print("Selected candidate has no numeric collectionId; skipped.")
            continue

        overrides[text_key] = collection_id
        write_overrides(overrides_path, overrides)
        print(f"Saved override: {text_key} -> {collection_id}")

    for row in unresolved_rows:
        text_key = row.get("text_key", "")
        print("\n--- Unresolved ---")
        print(f"text_key: {text_key}")
        print(f"album_artist: {row.get('album_artist', '')}")
        print(f"album: {row.get('album', '')}")
        print(f"rep_track: {row.get('rep_track', '')}")
        print(f"error: {row.get('error', '')}")
        try:
            value = input("Paste Apple Music album URL or collectionId (or skip/quit): ").strip()
        except EOFError:
            break
        if value.lower() == "quit":
            break
        if value.lower() == "skip" or not value:
            continue

        collection_id = parse_collection_id_from_text(value)
        if collection_id is None:
            print("Could not parse a numeric collectionId.")
            continue

        overrides[text_key] = collection_id
        write_overrides(overrides_path, overrides)
        print(f"Saved override: {text_key} -> {collection_id}")

    print(f"Final overrides count: {len(overrides)}")
    print(f"Wrote: {overrides_path}")


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
    resolve_parser.add_argument("--sleep-ms", type=int, default=1500, help="Sleep between albums (ms)")
    resolve_parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume using existing album_id_map.json (default true)",
    )
    resolve_parser.add_argument("--search-limit", type=int, default=25, help="Search results limit")
    resolve_parser.add_argument("--min-score", type=float, default=8.0, help="Minimum score to resolve songs")
    resolve_parser.add_argument(
        "--min-score-album",
        type=float,
        default=5.0,
        help="Minimum score to resolve album fallback search",
    )
    resolve_parser.add_argument(
        "--ambiguity-gap",
        type=float,
        default=0.75,
        help="Min gap between top scores",
    )
    resolve_parser.add_argument("--max-429-in-row", type=int, default=3, help="Max consecutive 429s")
    resolve_parser.add_argument("--cooldown-seconds", type=int, default=600, help="Cooldown after 429s")
    resolve_parser.add_argument("--max-retries", type=int, default=4, help="Max retries per request")
    resolve_parser.add_argument("--verbose-429", action="store_true", help="Log 429 handling")
    resolve_parser.add_argument("--verbose-http", action="store_true", help="Log HTTP requests")
    resolve_parser.add_argument("--lookup", action="store_true", help="Enable lookup hydration")
    resolve_parser.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress + flush map every N",
    )
    resolve_parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("out/overrides.json"),
        help="Local text_key->collectionId overrides file",
    )
    resolve_parser.add_argument(
        "--write-overrides",
        action="store_true",
        help="Allow resolve to write parsed override URLs when present",
    )
    resolve_parser.add_argument(
        "--album-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable album-only fallback search when needed (default true)",
    )
    resolve_parser.add_argument(
        "--fallback-only-on-empty",
        action="store_true",
        help="Run album fallback only when primary song search has 0 candidates",
    )
    resolve_parser.add_argument(
        "--prefer-explicit",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer explicit variants during tie-breaks",
    )
    resolve_parser.add_argument(
        "--prefer-non-deluxe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer non-deluxe variants during tie-breaks",
    )
    resolve_parser.add_argument(
        "--prefer-original",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Prefer original (non-remaster/non-anniversary) during tie-breaks",
    )

    review_parser = subparsers.add_parser(
        "review",
        help="Interactively review ambiguous/unresolved rows and build overrides",
    )
    review_parser.add_argument(
        "--ambiguous-csv",
        type=Path,
        default=Path("out/ambiguous.csv"),
        help="Ambiguous CSV from resolve",
    )
    review_parser.add_argument(
        "--unresolved-csv",
        type=Path,
        default=Path("out/unresolved.csv"),
        help="Unresolved CSV from resolve",
    )
    review_parser.add_argument(
        "--overrides",
        type=Path,
        default=Path("out/overrides.json"),
        help="Overrides JSON path",
    )
    review_parser.add_argument(
        "--mode",
        choices=("ambiguous", "unresolved", "both"),
        default="both",
        help="Rows to review",
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
            min_score_album=args.min_score_album,
            ambiguity_gap=args.ambiguity_gap,
            max_429_in_row=args.max_429_in_row,
            cooldown_seconds=args.cooldown_seconds,
            max_retries=args.max_retries,
            verbose_429=args.verbose_429,
            verbose_http=args.verbose_http,
            lookup=args.lookup,
            progress_every=args.progress_every,
            overrides_path=args.overrides,
            write_overrides_flag=args.write_overrides,
            album_fallback=args.album_fallback,
            fallback_only_on_empty=args.fallback_only_on_empty,
            prefer_explicit=args.prefer_explicit,
            prefer_non_deluxe=args.prefer_non_deluxe,
            prefer_original=args.prefer_original,
        )
        return

    if args.command == "review":
        try:
            review_rows(
                ambiguous_csv=args.ambiguous_csv,
                unresolved_csv=args.unresolved_csv,
                overrides_path=args.overrides,
                mode=args.mode,
            )
        except KeyboardInterrupt:
            print("\nReview interrupted.", file=sys.stderr)
        return


if __name__ == "__main__":
    main()
