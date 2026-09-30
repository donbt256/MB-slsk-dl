import json
import os
import re
import sys
import time
from pathlib import Path

from matcher import (
    MATCHER_VERSION,
    classify_candidates,
    rank_candidates,
    rank_releases,
    parent_path,
    release_id,
    has_audio_extension,
)
from soulseek import SoulseekClient, flatten_responses
from release import release_key


RELEASE_FILTER = os.environ.get("RELEASE_KEY")


STATE_PATH = Path("state/tracks.json")

SEARCH_TIMEOUT_MS = int(
    os.environ.get("SLSKD_SEARCH_TIMEOUT_MS", "12000")
)

SEARCH_WAIT_SECONDS = int(
    os.environ.get("SLSKD_SEARCH_WAIT_SECONDS", "20")
)

RESPONSE_LIMIT = int(
    os.environ.get("SLSKD_RESPONSE_LIMIT", "100")
)

FILE_LIMIT = int(
    os.environ.get("SLSKD_FILE_LIMIT", "10000")
)


def load_state():
    if not STATE_PATH.exists():
        raise SystemExit(f"Missing {STATE_PATH}")

    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    temporary = STATE_PATH.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            state,
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")

    temporary.replace(STATE_PATH)


def metadata(track):
    return track.get("metadata", track)


def track_key(track):
    data = metadata(track)

    return data.get("id") or (
        f"{data.get('artist', '')}\x1f"
        f"{data.get('album', '')}\x1f"
        f"{data.get('title', '')}"
    )


def acquisition_status(track):
    return track.setdefault(
        "acquisition",
        {},
    ).get("status", "pending")


def local_file_exists(track):
    acquisition = track.get("acquisition", {})
    if not isinstance(acquisition, dict):
        return False

    file_info = acquisition.get("file")
    if not isinstance(file_info, dict):
        return False

    path = (
        file_info.get("path")
        or file_info.get("local_path")
        or file_info.get("localPath")
    )

    return bool(path) and Path(path).is_file()


def should_skip_track(track):
    status = acquisition_status(track)

    if status == "published":
        return True

    if status in {"downloaded", "ready_to_publish"}:
        # Runner-local downloaded files do not survive between GitHub Actions
        # jobs/runs. If the recorded file is absent, search must reacquire it.
        return local_file_exists(track)

    return False


def album_key(track):
    data = metadata(track)

    artist = str(
        data.get("album_artist")
        or data.get("artist")
        or ""
    ).strip()

    album = str(
        data.get("album")
        or ""
    ).strip()

    if not artist or not album:
        return None

    return (
        artist.casefold(),
        album.casefold(),
    )


def release_year(track):
    """Return a four-digit release year from MusicBrainz-derived metadata."""
    data = metadata(track)

    for key in (
        "release_year",
        "year",
        "release_date",
        "date",
        "original_release_date",
    ):
        value = data.get(key)
        if value is None:
            continue

        match = re.search(r"\b(19\d{2}|20\d{2})\b", str(value))
        if match:
            return match.group(1)

    return None


def normalize_search_query(query):
    """
    Produce a Soulseek-friendly version of a query by treating punctuation
    as separators. Keep the original query as the first attempt because
    punctuation can occasionally be meaningful.
    """
    normalized = re.sub(r"[^\w\s]+", " ", query, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def build_album_groups(tracks):
    groups = {}

    for track in tracks:
        if should_skip_track(track):
            continue

        key = album_key(track)

        if key is None:
            continue

        if key not in groups:
            groups[key] = []

        groups[key].append(track)

    return groups


def search_one(client, query):
    # Soulseek/slskd can occasionally return an empty result snapshot while
    # the peer responses are still being resolved. Retry empty searches
    # before treating the release as genuinely unmatched.
    for attempt in range(1, 4):
        search_id = client.search(
            query,
            timeout_ms=SEARCH_TIMEOUT_MS,
            file_limit=FILE_LIMIT,
            response_limit=RESPONSE_LIMIT,
        )

        data = client.wait_for_search(
            search_id,
            timeout_seconds=SEARCH_WAIT_SECONDS,
        )

        if flatten_responses(data):
            if attempt > 1:
                print(
                    f"  Search retry {attempt} produced results."
                )
            return search_id, data, query

        if attempt < 3:
            print(
                f"  Search returned no candidates; "
                f"retrying ({attempt + 1}/3)..."
            )
            time.sleep(1)

    normalized = normalize_search_query(query)

    if normalized and normalized.casefold() != query.casefold():
        print(
            f"  Search returned no candidates; "
            f"retrying with punctuation normalized: {normalized}"
        )

        for attempt in range(1, 4):
            search_id = client.search(
                normalized,
                timeout_ms=SEARCH_TIMEOUT_MS,
                file_limit=FILE_LIMIT,
                response_limit=RESPONSE_LIMIT,
            )

            data = client.wait_for_search(
                search_id,
                timeout_seconds=SEARCH_WAIT_SECONDS,
            )

            if flatten_responses(data):
                if attempt > 1:
                    print(
                        f"  Normalized search retry {attempt} produced results."
                    )
                return search_id, data, normalized

            if attempt < 3:
                print(
                    f"  Normalized search returned no candidates; "
                    f"retrying ({attempt + 1}/3)..."
                )
                time.sleep(1)

    # Do not delete completed searches immediately. slskd can still be
    # finalizing/persisting a search in its background worker, and deleting
    # it here can race that finalization.
    return search_id, data, normalized if normalized else query


def ensure_search_state(track):
    return track.setdefault(
        "search",
        {
            "mode": None,
            "queries": [],
            "candidates": [],
            "release_candidates": [],
        },
    )


def compact_candidate(candidate):
    if not isinstance(candidate, dict):
        return candidate

    keys = (
        "candidate_id",
        "username",
        "filename",
        "size",
        "score",
        "extension",
        "candidate_title",
        "candidate_track_number",
        "alternate_terms",
    )

    return {
        key: candidate[key]
        for key in keys
        if key in candidate
    }


def compact_release(release):
    if not isinstance(release, dict):
        return release

    compacted = {
        key: release[key]
        for key in (
            "release_id",
            "username",
            "folder",
            "file_count",
            "expected_tracks",
            "matched_tracks",
            "coverage",
            "score",
            "alternate_count",
            "decision",
        )
        if key in release
    }

    compacted["matches"] = [
        {
            "track_id": item.get("track_id"),
            "title": item.get("title"),
            "track_number": item.get("track_number"),
            "candidate": compact_candidate(
                item.get("candidate", {})
            ),
        }
        for item in release.get("matches", [])
        if isinstance(item, dict)
    ]

    return compacted


def recover_durable_release(tracks):
    """
    Recover a previously matched complete release when a fresh Soulseek
    search transiently returns zero results.

    This is deliberately conservative: every requested track must retain
    a candidate, and all candidates must come from the same Soulseek user
    and remote folder.
    """
    grouped = {}

    for track in tracks:
        acquisition = track.get("acquisition", {})
        if not isinstance(acquisition, dict):
            return None

        match = acquisition.get("match")
        if not isinstance(match, dict):
            return None

        candidate = match.get("candidates", [])

        if not isinstance(candidate, list) or not candidate:
            return None

        if not isinstance(candidate[0], dict):
            return None

        candidate = candidate[0]

        username = str(candidate.get("username") or "")
        filename = str(candidate.get("filename") or "")
        folder = parent_path(filename)

        if not username or not folder or not has_audio_extension(filename):
            return None

        key = (username, folder)
        grouped.setdefault(key, []).append(
            (track, candidate)
        )

    complete = [
        items
        for items in grouped.values()
        if len(items) == len(tracks)
    ]

    if not complete:
        return None

    items = complete[0]

    matches = [
        {
            "track_id": metadata(track).get("id"),
            "title": metadata(track).get("title"),
            "track_number": metadata(track).get("track_number"),
            "candidate": compact_candidate(candidate),
        }
        for track, candidate in items
    ]

    score = sum(
        float(candidate.get("score") or 0)
        for _, candidate in items
    ) / len(items)

    return {
        "release_id": release_id(
            items[0][1].get("username"),
            parent_path(items[0][1].get("filename", "")),
        ),
        "username": items[0][1].get("username"),
        "folder": parent_path(items[0][1].get("filename", "")),
        "file_count": len(items),
        "expected_tracks": len(tracks),
        "matched_tracks": len(items),
        "coverage": 1.0,
        "exact_titles": 0,
        "exact_track_numbers": 0,
        "average_track_score": round(score, 2),
        "album_similarity": 0.0,
        "artist_similarity": 0.0,
        "alternate_count": 0,
        "score": round(score, 2),
        "decision": "accept",
        "matches": matches,
    }


def set_acquisition_match(
    track,
    candidate,
    status="matched",
):
    acquisition = track.setdefault(
        "acquisition",
        {},
    )

    acquisition["status"] = status

    acquisition["match"] = {
        "candidates": [compact_candidate(candidate)],
    }


def process_album_group(client, tracks, index, total):
    first = metadata(tracks[0])

    artist = first.get(
        "album_artist"
    ) or first.get(
        "artist",
        "",
    )

    album = first.get("album", "")

    query = f"{artist} {album}".strip()

    print()
    print(
        f"Album [{index}/{total}]: "
        f"{artist} - {album}"
    )
    print(
        f"  Tracks in request: {len(tracks)}"
    )
    print(
        f"  Search: {query}"
    )

    search_state = ensure_search_state(tracks[0])

    search_state["mode"] = "album"
    search_state["query"] = query

    search_id, data, effective_query = search_one(
        client,
        query,
    )

    raw_candidates = flatten_responses(data)

    print(
        f"  Search ID: {search_id}"
    )
    print(
        f"  Raw candidates: {len(raw_candidates)}"
    )

    if effective_query != query:
        print(
            f"  Effective search query: {effective_query}"
        )

    query = effective_query

    # Some Soulseek search terms can be filtered server-side. If the
    # artist+album query returns nothing, first retry using only the album
    # title and its MusicBrainz-derived release year. This deliberately
    # avoids sending the artist name again.
    year = release_year(tracks[0])
    year_query = f"{album} {year}".strip() if year else ""

    if not raw_candidates and year_query and year_query.casefold() != query.casefold():
        print(
            f"  Artist+album search returned no results; "
            f"retrying album+year: {year_query}"
        )
        year_search_id, year_data, year_effective_query = search_one(
            client,
            year_query,
        )
        year_candidates = flatten_responses(year_data)

        print(
            f"  Album+year search ID: {year_search_id}"
        )
        print(
            f"  Album+year raw candidates: {len(year_candidates)}"
        )

        if year_candidates:
            search_id = year_search_id
            raw_candidates = year_candidates
            query = year_effective_query

    # Album metadata often contains edition labels such as
    # "(2007 Remaster)" or "(Deluxe)". Soulseek shares frequently
    # omit those labels. If the exact metadata album query returns
    # nothing, retry once with parenthesized edition labels removed.
    simplified_album = re.sub(
        r"\s*\([^)]*\)",
        "",
        album,
    ).strip()
    fallback_query = f"{artist} {simplified_album}".strip()

    if not raw_candidates and fallback_query.casefold() != query.casefold():
        print(
            f"  Exact album search returned no results; "
            f"retrying: {fallback_query}"
        )
        fallback_search_id, fallback_data, fallback_effective_query = search_one(
            client,
            fallback_query,
        )
        fallback_candidates = flatten_responses(fallback_data)

        print(
            f"  Fallback search ID: {fallback_search_id}"
        )
        print(
            f"  Fallback raw candidates: {len(fallback_candidates)}"
        )

        if fallback_candidates:
            search_id = fallback_search_id
            raw_candidates = fallback_candidates
            query = fallback_effective_query

    for track in tracks:
        state = ensure_search_state(track)

        state["mode"] = "album"
        state["query"] = query
        state["queries"] = state.get(
            "queries",
            [],
        )

        state["queries"].append(
            {
                "search_id": search_id,
                "query": query,
                "candidate_count": len(raw_candidates),
                "timestamp": int(time.time()),
            }
        )

        state.pop("candidates", None)
        state["release_candidates"] = []

    if not raw_candidates:
        durable = recover_durable_release(tracks)

        if durable is not None:
            print(
                "  Fresh search returned no candidates; "
                "reusing the previously matched complete release."
            )

            compacted = compact_release(durable)

            for track, match in zip(
                tracks,
                durable["matches"],
            ):
                set_acquisition_match(
                    track,
                    match["candidate"],
                    "matched",
                )

                track.setdefault(
                    "matching",
                    {},
                )["deterministic"] = {
                    "version": MATCHER_VERSION,
                    "mode": "album",
                    "decision": "accept",
                    "release_id": durable["release_id"],
                    "release": compacted,
                }

                state = ensure_search_state(track)
                state["release_candidates"] = (
                    [compacted]
                    if track is tracks[0]
                    else []
                )

            return True

        for track in tracks:
            track.setdefault(
                "acquisition",
                {},
            )["status"] = "unmatched"

        print("  No candidates found.")
        return False

    releases = rank_releases(
        tracks,
        raw_candidates,
    )

    compacted_releases = [
        compact_release(release)
        for release in releases[:20]
    ]

    for index, track in enumerate(tracks):
        state = ensure_search_state(track)
        if index == 0:
            state["release_candidates"] = compacted_releases
        else:
            state["release_candidates"] = []

    if not releases:
        for track in tracks:
            track.setdefault(
                "acquisition",
                {},
            )["status"] = "unmatched"

        print("  No viable releases.")
        return False

    # Prefer the highest-ranked release that actually passes the
    # deterministic acceptance rules. The highest-scoring release overall
    # can be a deluxe/live/compilation release with extra or alternate
    # versions, even when a valid exact release is also present.
    accepted_releases = [
        release
        for release in releases
        if release.get("decision") == "accept"
    ]

    best = (
        accepted_releases[0]
        if accepted_releases
        else releases[0]
    )

    print(
        f"  Release candidates: {len(releases)}"
    )
    print(
        f"  Best release: "
        f"{best['folder']} "
        f"from {best['username']}"
    )
    print(
        f"  Matched: "
        f"{best['matched_tracks']}/"
        f"{best['expected_tracks']} tracks"
    )
    print(
        f"  Score: {best['score']}"
    )

    if accepted_releases:
        print(
            f"  Accepted release candidates: "
            f"{len(accepted_releases)}"
        )

    decision = best["decision"]

    if decision == "accept":
        print("  Decision: accept")

        matches_by_track = {
            match.get("track_id"): match["candidate"]
            for match in best.get("matches", [])
        }

        for track in tracks:
            data = metadata(track)
            track_id = data.get("id")
            candidate = matches_by_track.get(track_id)

            if candidate is None:
                track.setdefault("acquisition", {})["status"] = "unmatched"
                continue

            set_acquisition_match(track, candidate, "matched")

            track.setdefault("matching", {})["deterministic"] = {
                "version": MATCHER_VERSION,
                "mode": "album",
                "decision": "accept",
                "release_id": best["release_id"],
                "release": compact_release(best),
            }

    else:
        print("  Decision: reject")

        for index, track in enumerate(tracks):
            track.setdefault("matching", {})["deterministic"] = {
                "version": MATCHER_VERSION,
                "mode": "album",
                "decision": "reject",
                "release_id": best["release_id"],
                "candidates": (
                    [compact_release(release) for release in releases[:15]]
                    if index == 0
                    else []
                ),
            }
            track.setdefault("acquisition", {})["status"] = "unmatched"

    return True


def process_individual_track(
    client,
    track,
    index,
    total,
):
    data = metadata(track)

    artist = data.get("artist", "")
    title = data.get("title", "")

    query = f"{artist} {title}".strip()

    print()
    print(
        f"Track [{index}/{total}]: "
        f"{artist} - {title}"
    )
    print(
        f"  Search: {query}"
    )

    search_state = ensure_search_state(track)

    search_state["mode"] = "track"
    search_state["query"] = query

    search_id, result, effective_query = search_one(
        client,
        query,
    )

    raw_candidates = flatten_responses(result)

    print(
        f"  Search ID: {search_id}"
    )
    print(
        f"  Raw candidates: {len(raw_candidates)}"
    )

    if effective_query != query:
        print(
            f"  Effective search query: {effective_query}"
        )

    query = effective_query

    search_state["queries"] = search_state.get(
        "queries",
        [],
    )

    search_state["queries"].append(
        {
            "search_id": search_id,
            "query": query,
            "candidate_count": len(raw_candidates),
            "timestamp": int(time.time()),
        }
    )

    search_state.pop("candidates", None)
    search_state["release_candidates"] = []

    if not raw_candidates:
        track.setdefault(
            "acquisition",
            {},
        )["status"] = "unmatched"

        print("  No candidates found.")
        return False

    scored = rank_candidates(
        track,
        raw_candidates,
    )

    classification = classify_candidates(
        scored
    )

    track.setdefault(
        "matching",
        {},
    )["deterministic"] = {
        "version": MATCHER_VERSION,
        "mode": "track",
        "decision": classification["decision"],
        "candidates": [
            compact_candidate(candidate)
            for candidate in scored[:15]
        ],
    }

    decision = classification["decision"]

    if decision == "accept":
        best = scored[0]

        print(
            f"  Best: "
            f"{best['filename']} "
            f"from {best['username']} "
            f"(score={best['score']})"
        )

        set_acquisition_match(track, best, "matched")

    else:
        print("  No deterministic match met the acceptance criteria.")
        track.setdefault("acquisition", {})["status"] = "unmatched"

    return True


def main():
    state = load_state()

    tracks = state.get("tracks", [])

    if RELEASE_FILTER:
        tracks = [track for track in tracks if release_key(track) == RELEASE_FILTER]

    print(f"Loaded {len(tracks)} tracks for this release.")

    if not tracks:
        return

    base_url = os.environ.get(
        "SLSKD_URL",
        "http://127.0.0.1:5030",
    )

    api_key = os.environ.get(
        "SLSKD_API_KEY"
    )

    client = SoulseekClient(
        base_url=base_url,
        api_key=api_key,
    )

    album_groups = build_album_groups(
        tracks
    )

    album_groups = {
        key: group
        for key, group in album_groups.items()
        if len(group) >= 2
    }

    album_track_ids = {
        track_key(track)
        for group in album_groups.values()
        for track in group
    }

    album_total = len(album_groups)

    for index, group in enumerate(
        album_groups.values(),
        start=1,
    ):
        process_album_group(
            client,
            group,
            index,
            album_total,
        )

        save_state(state)

    print()
    print(
        f"Album searches completed: "
        f"{album_total}"
    )

    # Recompute individual work after album searches. Rejected/incomplete
    # album matches are deliberately retried track-by-track rather than
    # being silently left with status "unmatched".
    pending_individual = [
        track
        for track in tracks
        if (
            not should_skip_track(track)
            and (
                track_key(track)
                not in album_track_ids
                or acquisition_status(track) != "matched"
            )
        )
    ]

    individual_total = len(pending_individual)

    for index, track in enumerate(
        pending_individual,
        start=1,
    ):
        process_individual_track(
            client,
            track,
            index,
            individual_total,
        )

        save_state(state)

    print()
    print(
        f"Individual track searches completed: "
        f"{individual_total}"
    )

    save_state(state)

    print()
    print(
        "Search stage complete."
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrupted.",
            file=sys.stderr,
        )
        raise
