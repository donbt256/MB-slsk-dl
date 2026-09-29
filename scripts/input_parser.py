from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AlbumRequest:
    artist: str
    album: str


@dataclass(frozen=True)
class TrackRequest:
    artist: str
    album: str
    title: str


@dataclass(frozen=True)
class InputRequests:
    albums: list[AlbumRequest]
    tracks: list[TrackRequest]


def _string(value: Any, field: str, location: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{location}: '{field}' must be a string")

    value = value.strip()
    if not value:
        raise ValueError(f"{location}: '{field}' cannot be empty")

    return value


def _list(data: dict[str, Any], field: str) -> list[Any]:
    value = data.get(field, [])
    if value is None:
        return []

    if not isinstance(value, list):
        raise ValueError(f"'{field}' must be a YAML list")

    return value


def _album(value: Any, index: int) -> AlbumRequest:
    location = f"albums[{index}]"

    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")

    allowed = {"artist", "album"}
    unknown = set(value) - allowed
    if unknown:
        fields = ", ".join(sorted(str(item) for item in unknown))
        raise ValueError(f"{location}: unknown field(s): {fields}")

    return AlbumRequest(
        artist=_string(value.get("artist"), "artist", location),
        album=_string(value.get("album"), "album", location),
    )


def _track(value: Any, index: int) -> TrackRequest:
    location = f"tracks[{index}]"

    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")

    allowed = {"artist", "album", "title"}
    unknown = set(value) - allowed
    if unknown:
        fields = ", ".join(sorted(str(item) for item in unknown))
        raise ValueError(f"{location}: unknown field(s): {fields}")

    return TrackRequest(
        artist=_string(value.get("artist"), "artist", location),
        album=_string(value.get("album"), "album", location),
        title=_string(value.get("title"), "title", location),
    )


def parse_input_file(path: str | Path) -> InputRequests:
    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(f"Missing input file: {path}")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise ValueError(
            "The YAML root must be an object containing "
            "'albums' and/or 'tracks'."
        )

    allowed = {"albums", "tracks"}
    unknown = set(data) - allowed
    if unknown:
        fields = ", ".join(sorted(str(item) for item in unknown))
        raise ValueError(f"Unknown top-level field(s): {fields}")

    albums = [
        _album(item, index)
        for index, item in enumerate(_list(data, "albums"))
    ]

    tracks = [
        _track(item, index)
        for index, item in enumerate(_list(data, "tracks"))
    ]

    if not albums and not tracks:
        raise ValueError("The input file contains no albums or tracks.")

    return InputRequests(albums=albums, tracks=tracks)
