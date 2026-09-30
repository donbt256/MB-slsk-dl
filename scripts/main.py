import json
from pathlib import Path

from input_parser import parse_input_file
from musicbrainz import resolve_requests
from config import string


ROOT = Path(__file__).resolve().parent.parent
INPUT_FILE = ROOT / string("paths.input_file", default="input.yaml")
STATE_DIR = ROOT / "state"
TRACKS_FILE = ROOT / string("paths.state_file", default="state/tracks.json")


def load_existing_state():
    if not TRACKS_FILE.exists():
        return {}

    try:
        data = json.loads(TRACKS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {TRACKS_FILE}: {exc}"
        ) from exc

    return {
        track.get("metadata", {}).get("id"): track
        for track in data.get("tracks", [])
        if track.get("metadata", {}).get("id")
    }


def default_acquisition():
    return {
        "status": "pending",
        "attempts": 0,
        "match": None,
        "file": None,
        "library": None,
    }


def build_track_state(metadata, existing=None, sources=None):
    if existing:
        acquisition = existing.get(
            "acquisition",
            default_acquisition(),
        )
        enrichment = existing.get(
            "enrichment",
            {"artwork": None, "lyrics": None},
        )
    else:
        acquisition = default_acquisition()
        enrichment = {"artwork": None, "lyrics": None}

    return {
        "metadata": metadata,
        "sources": sources or {
            "release_ids": [],
            "track_ids": [],
        },
        "acquisition": acquisition,
        "enrichment": enrichment,
    }


def main():
    requests = parse_input_file(INPUT_FILE)

    existing_tracks = load_existing_state()
    existing_state = (
        json.loads(TRACKS_FILE.read_text(encoding="utf-8"))
        if TRACKS_FILE.exists()
        else {}
    )
    musicbrainz_cache = existing_state.get(
        "musicbrainz_cache",
        {},
    )

    resolved = resolve_requests(
        requests,
        cache=musicbrainz_cache,
    )

    current_tracks = resolved["tracks"]
    current_sources = resolved["track_sources"]

    tracks_by_id = dict(existing_tracks)

    for metadata in current_tracks:
        track_id = metadata["id"]
        existing = existing_tracks.get(track_id)
        tracks_by_id[track_id] = build_track_state(
            metadata,
            existing=existing,
            sources=current_sources.get(track_id),
        )

    current_track_ids = set(current_sources)
    for track_id, track in tracks_by_id.items():
        track["active_source"] = track_id in current_track_ids

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    output = {
        "version": 1,
        "musicbrainz_cache": resolved.get(
            "cache",
            musicbrainz_cache,
        ),
        "releases": resolved.get("releases", []),
        "tracks": list(tracks_by_id.values()),
    }

    TRACKS_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    preserved = len(existing_tracks)
    new = len(tracks_by_id) - preserved

    print()
    print(f"Resolved {len(current_tracks)} current unique tracks.")
    print(f"Preserved state for {preserved} existing tracks.")
    print(f"Created state for {new} new tracks.")
    print(f"Resolved {len(resolved['releases'])} release(s).")

    unresolved = resolved.get("unresolved", [])
    if unresolved:
        print()
        print(f"Unresolved album request(s): {len(unresolved)}")
        for item in unresolved:
            print(
                f"  {item['artist']} - {item['album']}: "
                f"{item['error']}"
            )

    print(f"Wrote {TRACKS_FILE}")


if __name__ == "__main__":
    main()
