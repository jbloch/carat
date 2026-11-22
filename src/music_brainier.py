"""
music_brainier - A helper library for MusicBrainz.

Provides a higher level and more pleasant programmatic API than the thin skin over the web API that is musicbrainzngs.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

from __future__ import annotations

__all__ = ['init', 'MusicBrainzMetadata', 'MusicBrainzTrack', 'fetch_candidate_metadata', 'fetch_metadata',
           'get_best_mb_candidate', 'find_release_group', 'generate_mb_art_urls']

import difflib
import re
from collections.abc import Iterator
from typing import NotRequired, TypedDict, cast

import requests

import logger
import musicbrainz_api as mb_api
from musicbrainz_api import Medium, ReleaseSummary
from util import normalize_for_fuzzy_comparison


def init(app_version: str) -> None:
    """Initializes the MusicBrainz API client with the required User-Agent."""
    mb_api.init(app_version)


class MusicBrainzMetadata(TypedDict):
    """
    A dictionary of release information returned by MusicBrainz.

    Attributes:
        title (str): The canonicalized title of the release
        artist (str): The canonicalized artist credit
        year (str): The 4-digit year of the release (e.g., '1973')
        mbid (str): The 36-character MusicBrainz Release ID
        medium_index (int): The 1-based index of the physical medium (disc)
        tracks (list[MusicBrainzTrack]): The ordered list of tracks on this medium
    """
    title: str
    artist: str
    year: str
    mbid: str
    medium_index: int
    tracks: list[MusicBrainzTrack]


class MusicBrainzTrack(TypedDict):
    """
    A dictionary of track information returned by the MusicBrainz API.

    Attributes:
        title (str): The sanitized title of the track (e.g., with ETI stripped)
        duration_ms (int): The duration of the track in milliseconds
        track_artist (str): The artist credit for this track, when supplied by MusicBrainz
    """
    title: str
    duration_ms: int
    track_artist: NotRequired[str]


# Cover Art Archive returns image records separate from MusicBrainz release data.
_CAAThumbnails = TypedDict("_CAAThumbnails", {"1200": str}, total=False)


class _CAAImage(TypedDict):
    image: str
    thumbnails: _CAAThumbnails
    front: NotRequired[bool]


class _CAAResponse(TypedDict, total=False):
    images: list[_CAAImage]


def fetch_candidate_metadata(artist: str, album: str) -> list[MusicBrainzMetadata]:
    """
    Returns the metadata for the candidate releases corresponding to the given (inexact) artist and album name.
    All the releases returned will come from the same release group.
    """
    logger.emit("\n[*] === STARTING METADATA FETCH ===")

    rg = find_release_group(artist, album)
    if not rg:
        logger.emit("    [-] No matching release group found. Aborting metadata fetch.")
        return []
    rg_id, rg_artist, rg_title = rg

    releases = find_releases_and_dates_for_release_group(rg_id, rg_title)
    logger.emit(f"    -> Filtered down to {len(releases)} matching release(s).")

    if not releases:
        logger.emit("    [-] No matching releases found. Aborting metadata fetch.")
        return []

    candidates = fetch_metadata_for_releases(releases, rg_id, rg_artist, rg_title)
    return candidates


def get_best_mb_candidate(target_artist: str, target_album: str, chapter_count: int,
                          target_duration_sec: float, candidates: list[MusicBrainzMetadata]) -> MusicBrainzMetadata | None:
    """
    Finds the best MusicBrainz candidate for a given artist, name, target chapter count, and target duration.
    Filters for exact track count matches or +1 preamble matches, prioritizing similar durations, lower MB medium index,
    and finally string similarity to the target artist and album names.
    """
    if not candidates:
        return None

    # Filter for valid matches (exact or +1 preamble)
    matched = [c for c in candidates if 0 <= (chapter_count - len(c['tracks'])) <= 1]
    if not matched:
        return None

    safe_target = normalize_for_fuzzy_comparison(f"{target_artist} {target_album}").replace(" ", "")

    def get_similarity(c: MusicBrainzMetadata) -> float:
        """Calculates a similarity score based on string similarity to the target artist and album."""
        safe_cand = normalize_for_fuzzy_comparison(f"{c['artist']} {c['title']}").replace(" ", "")
        return difflib.SequenceMatcher(None, safe_target, safe_cand).ratio()

    def get_duration_penalty(c: MusicBrainzMetadata) -> int:
        """Calculates a penalty based on total duration difference (in 30-second buckets)."""
        mb_duration_ms = sum(t.get('duration_ms') or 0 for t in c['tracks'])
        if mb_duration_ms == 0 or target_duration_sec == 0:
            return 0
        mb_sec = round(mb_duration_ms / 1000)

        return int(abs(target_duration_sec - mb_sec) // 30)

    def get_medium_penalty(c: MusicBrainzMetadata) -> int:
        """Penalizes bonus discs and non-primary mediums."""
        # Medium 1 gets 0 penalty. Medium 2 gets 1 penalty, etc.
        return max(0, c.get('medium_index', 1) - 1)

    # Sort Hierarchy:
    # 1. Duration Penalty (Filters out radically different tracklists)
    # 2. Medium Position (Prioritizes Disc 1 over Disc 2/3/4)
    # 3. Text Similarity (Fuzzy string match for exact album/artist text)
    matched.sort(key=lambda c: (
        get_duration_penalty(c),
        get_medium_penalty(c),
        -get_similarity(c)
    ))

    for i, c in enumerate(matched):
        bullet = "*" if i == 0 else "-"
        logger.emit(f"    {bullet} {c['title']} ({c.get('year', 'Unknown')}) [MBID {c.get('mbid', 'Unknown')} "
                    f"({c.get('medium_index', 1)})] [Scores: Duration={get_duration_penalty(c)},"
                    f" Medium={get_medium_penalty(c)}, Artist/Title={get_similarity(c) :.2f}]")

    return matched[0]


# The maximum number of releases to search for a good match for the user-supplied album and artist names
MAX_RELEASES_TO_SEARCH: int = 15


def find_release_group(artist: str, album: str) -> tuple[str, str, str] | None:
    """
    Finds the release group corresponding to the given artist and album name (which may be inexact).
    Searches by release rather than release group to bypass strict artist indexing.
    Prioritizes 'Album' release groups over 'Single' or 'EP' to prevent tracklist mismatches.

    Returns:
        A tuple containing (release_group_id, artist, title) if found, otherwise None.
    """
    # Priority mapping for release group types (lowest number wins)
    type_priority = {"Album": 1, "EP": 2, "Single": 3}

    for is_strict in [True, False]:
        query = f'artist:"{artist}" AND release:"{album}"' if is_strict else f'"{artist}" "{album}"'
        logger.emit(f"[*] Executing Query: {query}")
        try:
            res = mb_api.search_releases(query=query, limit=MAX_RELEASES_TO_SEARCH)

            valid_matches: list[tuple[int, str, str, str, str]] = []

            for r in res['release-list']:
                found_artist = r['artist-credit-phrase'].strip() or 'Unknown'
                found_album = r['title']

                # Evaluate matches for plausibility with "substring leniency"
                artist_match = _is_safe_match(artist, found_artist)
                album_match = _is_safe_match(album, found_album)

                if artist_match and album_match:
                    rg = r['release-group']
                    rg_type = rg.get('primary-type', 'Unknown')
                    priority = type_priority.get(rg_type, 4)
                    valid_matches.append((priority, rg['id'], found_artist, found_album, rg_type))
                else:
                    logger.emit(f"    [-] Rejected Candidate: {found_artist} - {found_album} "
                                f"(Artist Match: {artist_match}, Album Match: {album_match})")

            # If we found matches in this strictness pass, pick the best one and exit!
            if valid_matches:
                # Sort by our priority score (index 0 of our tuple)
                valid_matches.sort(key=lambda x: x[0])
                best_match = valid_matches[0]

                _, rg_id, rg_artist, rg_title, rg_type = best_match
                logger.emit(f"    [+] Match Found: {rg_artist} - {rg_title} (RG ID: {rg_id}, Type: {rg_type})\n")

                return rg_id, rg_artist, rg_title

        except mb_api.WebServiceError as e:
            logger.emit(f"    [!] API Error: {e}")

    return None


def find_releases_and_dates_for_release_group(rg_id: str, rg_title: str) -> list[tuple[str, str]]:
    """
    Returns release IDs and dates of releases of the given release group corresponding to all possible track-counts.
    Evaluates mediums individually to strictly match physical disc topology.
    """
    logger.emit(f"[*] Fetching all releases and mediums for Release Group: {rg_id} ({rg_title})")

    releases: list[ReleaseSummary] = []
    limit = 100
    offset = 0

    # 1. Fetch all releases by paginating through the browse_releases endpoint
    try:
        while True:
            result = mb_api.browse_releases(rg_id, limit, offset)
            batch = result['release-list']
            releases.extend(batch)

            if len(batch) < limit:
                break  # We've reached the end of the list
            offset += limit

        logger.emit(f"    -> API returned {len(releases)} releases in this group.")
    except mb_api.WebServiceError as e:
        logger.emit(f"    [!] Error fetching releases: {e}")
        return []

    unique_releases: dict[str, str] = {}
    seen_counts: set[int] = set()

    # 2. Map the mediums to find unique track counts
    for r in releases:
        mediums = r.get('medium-list', [])

        if not mediums:
            t_count = r.get('medium-track-count', 0)
            if t_count > 0 and t_count not in seen_counts:
                seen_counts.add(t_count)
                unique_releases[r['id']] = r.get('date', '')[:4]
            continue

        for m in mediums:
            m_count = m['track-count']
            if m_count > 0 and m_count not in seen_counts:
                seen_counts.add(m_count)
                unique_releases[r['id']] = r.get('date', '')[:4]

    logger.emit(f"    -> Identified unique track counts: {sorted(list(seen_counts))}")
    return [(r_id, date) for r_id, date in unique_releases.items()]


def _extract_medium_tracks(medium: Medium) -> list[MusicBrainzTrack]:
    """Extracts sanitized track metadata from a MusicBrainz medium."""
    tracks: list[MusicBrainzTrack] = []
    for track in medium.get('track-list', []):
        recording = track['recording']
        track_data: MusicBrainzTrack = {
            'title': sanitize_track_title(recording['title']),
            'duration_ms': int(recording.get('length') or 0)
        }
        # musicbrainzngs populates track artist-credit-phrase from recording when the track omits it
        track_artist = track.get('artist-credit-phrase', '').strip()
        if track_artist:
            track_data['track_artist'] = track_artist
        tracks.append(track_data)
    return tracks


def fetch_metadata_for_releases(release_ids_and_dates: list[tuple[str, str]],
                                rg_id: str, rg_artist: str, rg_title: str) -> list[MusicBrainzMetadata]:
    """Fetch the tracklists for the given release ids (and dates), which pertain to the given release group metadata"""
    logger.emit(f"\n[*] Fetching tracklists for {len(release_ids_and_dates)} matching MB releases in release group {rg_id}...")
    candidates: list[MusicBrainzMetadata] = []
    for rel_id, year in release_ids_and_dates:
        try:
            rel_info = mb_api.fetch_release_by_id(rel_id, includes=['recordings', 'artist-credits'])
            release = rel_info['release']

            # Treat EVERY medium as its own independent candidate
            for medium in release.get('medium-list', []):
                medium_tracks = _extract_medium_tracks(medium)
                if medium_tracks:
                    candidates.append({
                        'title': rg_title,
                        'artist': rg_artist,
                        'year': year or 'Unknown',
                        'mbid': rel_id,  # Specific release MBID instead of the Release Group ID
                        'medium_index': int(medium['position']),
                        'tracks': medium_tracks
                    })
        except mb_api.WebServiceError:
            continue
    logger.emit(f"[*] Metadata fetch complete. Found {len(candidates)} candidate MB mediums in release group.")
    return candidates


def sanitize_track_title(title: str) -> str:
    """
    Strips Extra Title Information (MusicBrainz ETI) from track titles safely.
    Removes parenthetical blocks ONLY if they contain known technical/mix keywords.
    Preserves structural parentheticals like "(Don't Fear) The Reaper".
    """
    # A blacklist of words that strongly indicate technical metadata rather than a song title
    eti_keywords = r'(mix|remix|remaster|master|version|edit|live|instrumental|demo|take|stereo|mono|surround|atmos|acoustic)'

    # Matches " (" or "(", followed by anything, the keyword (as a whole word), anything, and ")"
    pattern = re.compile(rf'\s*\([^)]*\b{eti_keywords}\b[^)]*\)', re.IGNORECASE)

    # Strip the ETI and clean up any lingering trailing spaces
    return pattern.sub('', title).strip()


def _is_safe_match(expected: str, found: str) -> bool:
    """
    Compares two strings for similarity after stripping all spaces and punctuation.
    Acts as a lenient gatekeeper: allows pure substrings (for truncated titles or
    collaborations) or highly similar strings (for typos/acronyms).
    """
    # Universal substring safety valve for messy artist collaborations or truncated titles
    # (e.g., "Scary Monsters" in "Scary Monsters (and Super Creeps)")
    if expected.lower() in found.lower():
        return True

    safe_expected = normalize_for_fuzzy_comparison(expected).replace(" ", "")
    safe_found = normalize_for_fuzzy_comparison(found).replace(" ", "")

    ratio = difflib.SequenceMatcher(None, safe_expected, safe_found).ratio()
    return ratio > 0.7


def fetch_metadata(mbid: str, medium_idx: int) -> MusicBrainzMetadata:
    """Fetches the metadata for a single medium of a release."""
    logger.emit(f"[*] Fetching metadata for MBID {mbid} (Medium {medium_idx})...")

    rel_info = mb_api.fetch_release_by_id(mbid, includes=['recordings', 'artist-credits'])
    release = rel_info['release']
    medium_info = next(
        (m for m in release.get('medium-list', []) if int(m['position']) == medium_idx),
        None,
    )
    if medium_info is None:
        raise ValueError(f"Medium {medium_idx} not found in release {mbid}.")

    medium_tracks = _extract_medium_tracks(medium_info)

    metadata: MusicBrainzMetadata = {
        'title': release['title'],
        'artist': release.get('artist-credit-phrase', '').strip() or 'Unknown',
        'year': release.get('date', 'Unknown')[:4],
        'mbid': mbid,
        'medium_index': medium_idx,
        'tracks': medium_tracks
    }

    logger.emit(f"[+] Found Release: {metadata['artist']} - {metadata['title']} ({len(metadata['tracks'])} tracks)")
    return metadata


def generate_mb_art_urls(artist: str, album: str, mbid: str | None = None) -> Iterator[str]:
    """ Yields candidate CoverArtArchive URLs in priority order. """
    logger.emit(f"[*] Searching MusicBrainz for {artist} - {album}...")
    batches: list[list[ReleaseSummary]] = []

    try:
        if mbid:
            # 1. Exact match (Use a fake future date to ensure it sorts to the top)
            batches.append([{'id': mbid, 'date': '9999'}])

            # 2. Get siblings
            rel_info = mb_api.fetch_release_by_id(mbid, includes=["release-groups"])
            release_group = rel_info['release'].get('release-group')
            if release_group is None:
                raise KeyError('release-group')
            rg_id = release_group['id']
        else:
            # 3. No MBID, search by text
            release_groups = mb_api.search_release_groups(artist=artist, releasegroup=album)
            if release_groups['release-group-count'] == 0:
                return
            rg_id = release_groups['release-group-list'][0]['id']

        # Get all releases in the group
        release_group = mb_api.fetch_release_group_by_id(rg_id, includes=["releases"])['release-group']
        releases = release_group.get('release-list')
        if releases is None:
            raise KeyError('release-list')

        # Batch them by priority
        digital = [r for r in releases if r.get('status') == 'Official' and r.get('packaging') is None]
        official = [r for r in releases if r.get('status') == 'Official' and r not in digital]
        batches.extend([digital, official])

        # Yield URLs one by one
        for batch in batches:
            batch.sort(key=lambda x: x.get('date', '0000'), reverse=True)
            for r in batch:
                mb_id = r['id']

                # Direct CAA network call (Skips the redundant MB API check!)
                resp = requests.get(f"https://coverartarchive.org/release/{mb_id}", timeout=10)
                if resp.status_code == 200:
                    logger.emit(f"  [+] Found CAA metadata for: {mb_id}")
                    response = cast(_CAAResponse, resp.json())
                    for img_entry in response.get('images', []):
                        if img_entry.get('front'):
                            # YIELD pauses this function and hands the URL back to client, e.g., get_cover_art.py
                            yield img_entry['thumbnails'].get('1200') or img_entry['image']

    except (mb_api.MusicBrainzError, KeyError, IndexError, requests.RequestException) as e:
        logger.emit(f"  [!] MusicBrainz/CAA API Error: {e}")
