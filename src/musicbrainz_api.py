"""
Typed, retrying adapter for the MusicBrainz API calls used by music_brainier.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

from __future__ import annotations

__all__ = [
    "ArtistCreditEntity",
    "MusicBrainzError",
    "ReleaseSummary",
    "WebServiceError",
    "browse_releases",
    "fetch_release_by_id",
    "fetch_release_group_by_id",
    "init",
    "search_release_groups",
    "search_releases",
]

import time
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, Protocol, TypedDict, TypeVar, cast

import musicbrainzngs as mb # pyright: ignore[reportMissingTypeStubs]

_P = ParamSpec("_P")
_R = TypeVar("_R")


# MusicBrainz uses either a flat artist string or a list of credited fragments.
class _ArtistCreditArtist(TypedDict, total=False):
    name: str


class _ArtistCreditFragment(TypedDict, total=False):
    name: str
    artist: _ArtistCreditArtist
    joinphrase: str


_ArtistCredit = str | list[str | _ArtistCreditFragment | None] | None
ArtistCreditEntity = TypedDict(
    "ArtistCreditEntity",
    {"artist-credit": _ArtistCredit},
    total=False,
)


# These are the nested response fields music_brainier reads; other API fields are intentionally omitted.
_Recording = TypedDict(
    "_Recording",
    {
        "title": str,
        "length": int | str,
        "artist-credit": _ArtistCredit,
    },
    total=False,
)


_Track = TypedDict(
    "_Track",
    {
        "recording": _Recording,
        "artist-credit": _ArtistCredit,
    },
    total=False,
)
_Medium = TypedDict(
    "_Medium",
    {
        "position": int | str,
        "track-list": list[_Track],
    },
    total=False,
)
_BrowseMedium = TypedDict("_BrowseMedium", {"track-count": int | str}, total=False)


class _ReleaseID(TypedDict):
    id: str


_ReleaseSummaryFields = TypedDict(
    "_ReleaseSummaryFields",
    {
        "date": str,
        "status": str,
        "packaging": str | None,
        "track-count": int | str,
        "medium-track-count": int | str,
        "medium-list": list[_BrowseMedium],
    },
    total=False,
)


class ReleaseSummary(_ReleaseID, _ReleaseSummaryFields):
    """Release fields used for track counts and cover-art sibling selection."""


_ReleaseGroupFields = TypedDict(
    "_ReleaseGroupFields",
    {
        "primary-type": str,
        "release-list": list[ReleaseSummary],
    },
    total=False,
)


class _ReleaseGroup(_ReleaseID, _ReleaseGroupFields):
    pass


_ReleaseDetails = TypedDict(
    "_ReleaseDetails",
    {
        "title": str,
        "date": str,
        "artist-credit": _ArtistCredit,
        "medium-list": list[_Medium],
        "release-group": _ReleaseGroup,
    },
    total=False,
)
_ReleaseByIDResponse = TypedDict("_ReleaseByIDResponse", {"release": _ReleaseDetails})
_ReleaseGroupByIDResponse = TypedDict(
    "_ReleaseGroupByIDResponse",
    {"release-group": _ReleaseGroup},
)
_ReleaseSearchResult = TypedDict(
    "_ReleaseSearchResult",
    {
        "title": str,
        "artist-credit": _ArtistCredit,
        "release-group": _ReleaseGroup,
    },
    total=False,
)
_ReleaseSearchResponse = TypedDict(
    "_ReleaseSearchResponse",
    {"release-list": list[_ReleaseSearchResult]},
)
_ReleaseGroupSearchResult = TypedDict("_ReleaseGroupSearchResult", {"id": str})
_ReleaseGroupSearchResponse = TypedDict(
    "_ReleaseGroupSearchResponse",
    {
        "release-group-count": int,
        "release-group-list": list[_ReleaseGroupSearchResult],
    },
)
_BrowseReleasesResponse = TypedDict(
    "_BrowseReleasesResponse",
    {"release-list": list[ReleaseSummary]},
)


# This protocol describes only the untyped dependency surface music_brainier calls.
class _MusicBrainzAPI(Protocol):
    MusicBrainzError: type[Exception]
    WebServiceError: type[Exception]

    def set_useragent(self, app: str, version: str, contact: str | None = None) -> None: ...

    def get_release_by_id(
        self,
        rel_id: str,
        includes: list[str] | None = None,
    ) -> _ReleaseByIDResponse: ...

    def get_release_group_by_id(
        self,
        rg_id: str,
        includes: list[str] | None = None,
    ) -> _ReleaseGroupByIDResponse: ...

    def search_releases(self, query: str, limit: int) -> _ReleaseSearchResponse: ...

    def search_release_groups(self, artist: str, releasegroup: str) -> _ReleaseGroupSearchResponse: ...

    def browse_releases(
        self,
        release_group: str,
        includes: list[str],
        limit: int,
        offset: int,
    ) -> _BrowseReleasesResponse: ...


_mb_api = cast(_MusicBrainzAPI, mb)
MusicBrainzError: type[Exception] = _mb_api.MusicBrainzError
WebServiceError: type[Exception] = _mb_api.WebServiceError


def init(app_version: str) -> None:
    """Initializes the MusicBrainz API client with Carat's User-Agent."""
    _mb_api.set_useragent("carat - concise atmos rip automation tool", app_version, "josh@bloch.us")


def _retry_mb_api(
    retries: int = 3,
    delay: float = 2.0,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Retry API calls after transient MusicBrainz web-service errors."""
    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        @wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except _mb_api.WebServiceError:
                    attempt += 1
                    if attempt >= retries:
                        raise
                    time.sleep(delay)

        return wrapper

    return decorator


@_retry_mb_api()
def fetch_release_by_id(rel_id: str, includes: list[str] | None = None) -> _ReleaseByIDResponse:
    return _mb_api.get_release_by_id(rel_id, includes=includes or [])


@_retry_mb_api()
def fetch_release_group_by_id(
    rg_id: str,
    includes: list[str] | None = None,
) -> _ReleaseGroupByIDResponse:
    return _mb_api.get_release_group_by_id(rg_id, includes=includes or [])


@_retry_mb_api()
def search_releases(query: str, limit: int) -> _ReleaseSearchResponse:
    return _mb_api.search_releases(query=query, limit=limit)


@_retry_mb_api()
def search_release_groups(artist: str, releasegroup: str) -> _ReleaseGroupSearchResponse:
    return _mb_api.search_release_groups(artist=artist, releasegroup=releasegroup)


@_retry_mb_api()
def browse_releases(rg_id: str, limit: int, offset: int) -> _BrowseReleasesResponse:
    return _mb_api.browse_releases(release_group=rg_id, includes=["media"], limit=limit, offset=offset)
