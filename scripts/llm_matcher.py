import json
import os
from pathlib import Path

from openai import OpenAI
from release import release_key


RELEASE_FILTER = os.environ.get("RELEASE_KEY")

STATE_PATH = Path("state/tracks.json")

MODEL = os.environ.get(
    "OPENAI_MATCH_MODEL",
    "gpt-5-nano",
)

MAX_CANDIDATES = int(
    os.environ.get(
        "OPENAI_MAX_CANDIDATES",
        "15",
    )
)


def load_state():
    with STATE_PATH.open(
        "r",
        encoding="utf-8",
    ) as handle:
        return json.load(handle)


def save_state(state):
    temporary = STATE_PATH.with_suffix(".tmp")

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as handle:
        json.dump(
            state,
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    temporary.replace(STATE_PATH)


def metadata(track):
    return track.get(
        "metadata",
        track,
    )


def candidate_summary(candidate):
    return {
        "candidate_id": candidate.get(
            "candidate_id",
            "",
        ),
        "username": candidate.get(
            "username",
            "",
        ),
        "filename": candidate.get(
            "filename",
            "",
        ),
        "size": candidate.get(
            "size",
        ),
        "extension": candidate.get(
            "extension",
            "",
        ),
        "score": candidate.get(
            "score",
            0,
        ),
        "candidate_title": candidate.get(
            "candidate_title",
            "",
        ),
        "candidate_track_number": candidate.get(
            "candidate_track_number",
        ),
        "alternate_terms": candidate.get(
            "alternate_terms",
            [],
        ),
    }


def release_summary(release):
    return {
        "release_id": release.get(
            "release_id",
            "",
        ),
        "username": release.get(
            "username",
            "",
        ),
        "folder": release.get(
            "folder",
            "",
        ),
        "file_count": release.get(
            "file_count",
            0,
        ),
        "expected_tracks": release.get(
            "expected_tracks",
            0,
        ),
        "matched_tracks": release.get(
            "matched_tracks",
            0,
        ),
        "coverage": release.get(
            "coverage",
            0,
        ),
        "score": release.get(
            "score",
            0,
        ),
        "alternate_count": release.get(
            "alternate_count",
            0,
        ),
        "matches": [
            {
                "track_id": item.get(
                    "track_id"
                ),
                "title": item.get(
                    "title"
                ),
                "track_number": item.get(
                    "track_number"
                ),
                "candidate": candidate_summary(
                    item.get(
                        "candidate",
                        {},
                    )
                ),
            }
            for item in release.get(
                "matches",
                [],
            )
        ],
    }


def call_llm_for_track(
    client,
    track,
    candidates,
):
    data = metadata(track)

    prompt = {
        "task": (
            "Determine which Soulseek candidate, if any, "
            "is most likely to be the exact metadata recording. "
            "Do not select live, acoustic, demo, remix, piano, "
            "instrumental, karaoke, radio edit, tribute, or "
            "other alternate versions unless the metadata target "
            "itself is explicitly that version."
        ),
        "metadata": {
            "artist": data.get("artist", ""),
            "artists": data.get("artists", []),
            "album": data.get("album", ""),
            "album_artist": data.get(
                "album_artist",
                "",
            ),
            "title": data.get("title", ""),
            "track_number": data.get(
                "track_number"
            ),
            "disc_number": data.get(
                "disc_number"
            ),
            "duration_ms": data.get(
                "duration_ms"
            ),
            "release_date": data.get(
                "release_date",
                "",
            ),
            "isrc": data.get(
                "isrc",
                "",
            ),
        },
        "candidates": [
            candidate_summary(candidate)
            for candidate in candidates[:MAX_CANDIDATES]
        ],
        "rules": [
            "Only choose a candidate_id that appears in candidates.",
            "Prefer exact title matches.",
            "Prefer matching artist and album directories.",
            "Prefer matching track numbers.",
            "Treat live, acoustic, demo, remix, piano, and similar terms as strong evidence of a different recording.",
            "Do not assume that a high deterministic score proves an exact recording.",
            "If none is sufficiently convincing, reject.",
        ],
    }

    schema = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": [
                    "accept",
                    "reject",
                ],
            },
            "candidate_id": {
                "type": "string",
            },
            "confidence": {
                "type": "number",
            },
            "version_classification": {
                "type": "string",
                "enum": [
                    "standard",
                    "live",
                    "acoustic",
                    "demo",
                    "remix",
                    "instrumental",
                    "piano",
                    "edit",
                    "other_alternate",
                    "unknown",
                ],
            },
            "reason": {
                "type": "string",
            },
        },
        "required": [
            "decision",
            "candidate_id",
            "confidence",
            "version_classification",
            "reason",
        ],
        "additionalProperties": False,
    }

    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You are a music metadata matching system. "
                    "You are screening existing Soulseek search "
                    "results. Never invent a candidate."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    prompt,
                    ensure_ascii=False,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "track_match",
                "strict": True,
                "schema": schema,
            }
        },
    )

    return json.loads(
        response.output_text
    )


def call_llm_for_album(
    client,
    tracks,
    releases,
):
    first = metadata(tracks[0])

    album_tracks = []

    for track in tracks:
        data = metadata(track)

        album_tracks.append(
            {
                "track_id": data.get("id"),
                "title": data.get("title", ""),
                "track_number": data.get(
                    "track_number"
                ),
                "disc_number": data.get(
                    "disc_number"
                ),
            }
        )

    prompt = {
        "task": (
            "Select the Soulseek release that most likely "
            "corresponds to the exact metadata album. The "
            "release should contain the expected tracks in "
            "the expected order and should not be a live, "
            "acoustic, demo, remix, piano, or other alternate "
            "release unless metadata's target album explicitly "
            "indicates that version."
        ),
        "metadata_album": {
            "artist": first.get("artist", ""),
            "album_artist": first.get(
                "album_artist",
                "",
            ),
            "album": first.get("album", ""),
            "release_date": first.get(
                "release_date",
                "",
            ),
            "tracks": album_tracks,
        },
        "release_candidates": [
            release_summary(release)
            for release in releases[:MAX_CANDIDATES]
        ],
        "rules": [
            "Only choose a release_id from the supplied candidates.",
            "Prefer complete tracklists.",
            "Prefer exact track titles.",
            "Prefer matching track numbers.",
            "Prefer matching album and artist names.",
            "Penalize live, acoustic, demo, remix, piano, and similar alternate versions.",
            "Reject if no release is sufficiently convincing.",
        ],
    }

    schema = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": [
                    "accept",
                    "reject",
                ],
            },
            "release_id": {
                "type": "string",
            },
            "confidence": {
                "type": "number",
            },
            "reason": {
                "type": "string",
            },
        },
        "required": [
            "decision",
            "release_id",
            "confidence",
            "reason",
        ],
        "additionalProperties": False,
    }

    response = client.responses.create(
        model=MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "You are a music release matching system. "
                    "You are screening existing Soulseek results. "
                    "Never invent a release."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    prompt,
                    ensure_ascii=False,
                ),
            },
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "album_match",
                "strict": True,
                "schema": schema,
            }
        },
    )

    return json.loads(
        response.output_text
    )


def apply_track_result(
    track,
    result,
):
    matching = track.setdefault(
        "matching",
        {},
    )

    matching["llm"] = result

    acquisition = track.setdefault(
        "acquisition",
        {},
    )

    deterministic = matching.get(
        "deterministic",
        {},
    )

    candidates = deterministic.get(
        "candidates",
        [],
    )

    selected = None

    candidate_id = result.get(
        "candidate_id",
        "",
    )

    for candidate in candidates:
        if candidate.get(
            "candidate_id"
        ) == candidate_id:
            selected = candidate
            break

    if (
        result.get("decision") == "accept"
        and float(
            result.get("confidence", 0)
        ) >= 85
        and selected is not None
    ):
        acquisition["status"] = "matched"
        acquisition["match"] = {
            "candidates": [
                selected
            ]
        }

        return True

    acquisition["status"] = "needs_review"

    return False


def apply_album_result(
    tracks,
    result,
):
    first = tracks[0]

    matching = first.setdefault(
        "matching",
        {},
    )

    matching["llm"] = result

    if (
        result.get("decision") != "accept"
        or float(
            result.get("confidence", 0)
        ) < 85
    ):
        for track in tracks:
            track.setdefault(
                "acquisition",
                {},
            )["status"] = "needs_review"

        return False

    release_id = result.get(
        "release_id",
        "",
    )

    deterministic = matching.get(
        "deterministic",
        {},
    )

    releases = deterministic.get(
        "candidates",
        [],
    )

    selected_release = None

    for release in releases:
        if release.get(
            "release_id"
        ) == release_id:
            selected_release = release
            break

    if selected_release is None:
        for track in tracks:
            track.setdefault(
                "acquisition",
                {},
            )["status"] = "needs_review"

        return False

    matches_by_track = {
        item.get("track_id"): item.get(
            "candidate"
        )
        for item in selected_release.get(
            "matches",
            [],
        )
    }

    all_matched = True

    for track in tracks:
        data = metadata(track)
        track_id = data.get("id")

        candidate = matches_by_track.get(
            track_id
        )

        if candidate is None:
            track.setdefault(
                "acquisition",
                {},
            )["status"] = "needs_review"

            all_matched = False
            continue

        track_matching = track.setdefault(
            "matching",
            {},
        )

        track_matching["llm"] = result

        acquisition = track.setdefault(
            "acquisition",
            {},
        )

        acquisition["status"] = "matched"

        acquisition["match"] = {
            "release_id": selected_release[
                "release_id"
            ],
            "release": selected_release,
            "candidates": [
                candidate
            ],
        }

    return all_matched


def main():
    api_key = os.environ.get(
        "OPENAI_API_KEY"
    )

    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set."
        )

    state = load_state()

    tracks = state.get(
        "tracks",
        [],
    )

    if RELEASE_FILTER:
        tracks = [track for track in tracks if release_key(track) == RELEASE_FILTER]

    client = OpenAI(
        api_key=api_key
    )

    processed_album_groups = set()

    for track in tracks:
        matching = track.get(
            "matching",
            {},
        )

        deterministic = matching.get(
            "deterministic",
            {},
        )

        if deterministic.get(
            "decision"
        ) != "llm":
            continue

        mode = deterministic.get(
            "mode"
        )

        if mode == "album":
            metadata_data = metadata(track)

            key = (
                metadata_data.get(
                    "album_artist"
                )
                or metadata_data.get(
                    "artist"
                ),
                metadata_data.get(
                    "album"
                ),
            )

            if key in processed_album_groups:
                continue

            group = [
                candidate_track
                for candidate_track in tracks
                if (
                    metadata(
                        candidate_track
                    ).get(
                        "album_artist"
                    )
                    or metadata(
                        candidate_track
                    ).get(
                        "artist"
                    ),
                    metadata(
                        candidate_track
                    ).get(
                        "album"
                    ),
                ) == key
            ]

            releases = deterministic.get(
                "candidates",
                [],
            )

            print()
            print(
                "LLM album screening:"
            )
            print(
                f"  {key[0]} - {key[1]}"
            )
            print(
                f"  Candidates: "
                f"{len(releases)}"
            )

            result = call_llm_for_album(
                client,
                group,
                releases,
            )

            print(
                f"  Decision: "
                f"{result.get('decision')}"
            )
            print(
                f"  Confidence: "
                f"{result.get('confidence')}"
            )
            print(
                f"  Release: "
                f"{result.get('release_id')}"
            )

            apply_album_result(
                group,
                result,
            )

            processed_album_groups.add(
                key
            )

        elif mode == "track":
            candidates = deterministic.get(
                "candidates",
                [],
            )

            result = call_llm_for_track(
                client,
                track,
                candidates,
            )

            data = metadata(track)

            print()
            print(
                "LLM track screening:"
            )
            print(
                f"  {data.get('artist')} - "
                f"{data.get('title')}"
            )
            print(
                f"  Decision: "
                f"{result.get('decision')}"
            )
            print(
                f"  Confidence: "
                f"{result.get('confidence')}"
            )
            print(
                f"  Candidate: "
                f"{result.get('candidate_id')}"
            )

            apply_track_result(
                track,
                result,
            )

        save_state(state)

    save_state(state)

    print()
    print(
        "LLM screening complete."
    )


if __name__ == "__main__":
    main()