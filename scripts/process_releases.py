import json
import os
import subprocess
import sys
from pathlib import Path

from release import group_releases


STATE_PATH = Path("state/tracks.json")

SUCCESS_STATUSES = {
    "downloaded",
    "ready_to_publish",
    "published",
}


def load_state():
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)



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


def compact_state(state):
    changed = False

    album_seen = set()

    for track in state.get("tracks", []):
        metadata = track.get("metadata", {})
        album_key = (
            str(
                metadata.get("album_artist")
                or metadata.get("artist")
                or ""
            ).casefold(),
            str(
                metadata.get("album")
                or ""
            ).casefold(),
        )
        first_album_track = album_key not in album_seen
        if album_key != ("", ""):
            album_seen.add(album_key)

        search = track.get("search")
        if isinstance(search, dict):
            if "candidates" in search:
                search.pop("candidates", None)
                changed = True

            releases = search.get("release_candidates")
            if isinstance(releases, list):
                compacted = (
                    [
                        compact_release(release)
                        for release in releases[:20]
                    ]
                    if first_album_track
                    else []
                )
                if compacted != releases:
                    search["release_candidates"] = compacted
                    changed = True

        matching = track.get("matching")
        if isinstance(matching, dict):
            deterministic = matching.get("deterministic")
            if isinstance(deterministic, dict):
                mode = deterministic.get("mode")
                candidates = deterministic.get("candidates")

                if isinstance(candidates, list):
                    if mode == "album":
                        compacted = (
                            [
                                compact_release(release)
                                for release in candidates[:15]
                            ]
                            if first_album_track
                            else []
                        )
                    else:
                        compacted = [
                            compact_candidate(candidate)
                            for candidate in candidates[:15]
                        ]

                    if compacted != candidates:
                        deterministic["candidates"] = compacted
                        changed = True

                if mode == "album" and not first_album_track:
                    if "release" in deterministic:
                        deterministic.pop("release", None)
                        changed = True
                elif isinstance(deterministic.get("release"), dict):
                    compacted_release = compact_release(
                        deterministic["release"]
                    )
                    if compacted_release != deterministic["release"]:
                        deterministic["release"] = compacted_release
                        changed = True

        acquisition = track.get("acquisition")
        if isinstance(acquisition, dict):
            attempts = acquisition.get("download_attempts")
            if isinstance(attempts, list) and len(attempts) > 10:
                acquisition["download_attempts"] = attempts[-10:]
                changed = True

            match = acquisition.get("match")
            if isinstance(match, dict):
                candidates = match.get("candidates")
                if isinstance(candidates, list):
                    compacted = [
                        compact_candidate(candidate)
                        for candidate in candidates[:1]
                    ]
                    if compacted != candidates:
                        match["candidates"] = compacted
                        changed = True

                if "release" in match:
                    match.pop("release", None)
                    changed = True

    return changed


def save_state(state):
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(
            state,
            handle,
            indent=2,
            ensure_ascii=False,
        )
        handle.write("\n")


def save_checkpoint(label):
    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )

    subprocess.run(
        ["git", "add", str(STATE_PATH)],
        check=True,
    )

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"]
    )

    if result.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", f"Checkpoint {label}"],
            check=True,
        )

        pushed = False
        for attempt in range(1, 4):
            result = subprocess.run(
                ["git", "push", "origin", "HEAD:main"],
            )
            if result.returncode == 0:
                pushed = True
                break

            print(
                f"Checkpoint push rejected (attempt {attempt}/3); "
                "rebasing onto the current remote main...",
                flush=True,
            )
            fetch = subprocess.run(
                ["git", "fetch", "origin", "main"],
            )
            if fetch.returncode != 0:
                break

            rebase = subprocess.run(
                ["git", "rebase", "origin/main"],
            )
            if rebase.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"])
                break

        if not pushed:
            raise RuntimeError(
                f"Could not push checkpoint after 3 attempts: {label}"
            )

        print(f"Checkpoint pushed: {label}", flush=True)
    else:
        print(f"No state changes to checkpoint: {label}", flush=True)


class FatalStageError(RuntimeError):
    pass


def run_stage(script, env):
    command = [sys.executable, script]

    print("", flush=True)
    print(f"=== Running {script} ===", flush=True)

    result = subprocess.run(
        command,
        env=env,
    )

    if result.returncode != 0:
        if script == "scripts/llm_matcher.py" and result.returncode == 78:
            raise FatalStageError(
                "OpenAI authentication failed. The OPENAI_API_KEY GitHub secret "
                "must be replaced before LLM matching can continue."
            )

        raise RuntimeError(
            f"{script} failed with exit code {result.returncode}"
        )


def local_file_exists(track):
    file_info = track.get("acquisition", {}).get("file")
    if not isinstance(file_info, dict):
        return False

    path = (
        file_info.get("path")
        or file_info.get("local_path")
        or file_info.get("localPath")
    )

    return bool(path) and Path(path).is_file()


def track_id(track):
    return str(
        track.get("metadata", {}).get("id")
        or ""
    )


def main():
    state = load_state()
    all_tracks = state.get("tracks", [])

    releases = group_releases(all_tracks)

    print(
        f"Found {len(all_tracks)} tracks in "
        f"{len(releases)} release(s).",
        flush=True,
    )

    if compact_state(state):
        save_state(state)
        save_checkpoint("Compact acquisition state")

    failed = False

    for index, release in enumerate(releases, start=1):
        label = release["label"]
        key = release["key"]
        ids = {
            track_id(track)
            for track in release["tracks"]
            if track_id(track)
        }

        current_state = load_state()
        current_tracks = [
            track
            for track in current_state.get("tracks", [])
            if track_id(track) in ids
        ]

        if not current_tracks:
            print(
                f"Skipping {label}: release tracks are no longer in state.",
                flush=True,
            )
            continue

        if all(
            track.get("acquisition", {}).get("status") == "published"
            for track in current_tracks
        ):
            print(
                f"[{index}/{len(releases)}] Already published: {label}",
                flush=True,
            )
            continue

        print("", flush=True)
        print(
            f"=== Release [{index}/{len(releases)}]: {label} ===",
            flush=True,
        )

        env = os.environ.copy()
        env["RELEASE_KEY"] = key

        try:
            run_stage("scripts/search.py", env)
            run_stage("scripts/llm_matcher.py", env)
            run_stage("scripts/download.py", env)

            current_state = load_state()
            current_tracks = [
                track
                for track in current_state.get("tracks", [])
                if track_id(track) in ids
            ]

            incomplete = [
                track
                for track in current_tracks
                if (
                    track.get("acquisition", {}).get("status")
                    not in SUCCESS_STATUSES
                    or (
                        track.get("acquisition", {}).get("status")
                        in {"downloaded", "ready_to_publish"}
                        and not local_file_exists(track)
                    )
                )
            ]

            if incomplete:
                names = [
                    track.get("metadata", {}).get("title", "?")
                    for track in incomplete
                ]
                raise RuntimeError(
                    "Release is not fully downloaded or its local files are missing: "
                    + ", ".join(names)
                )

            run_stage("scripts/publish.py", env)

            current_state = load_state()
            current_tracks = [
                track
                for track in current_state.get("tracks", [])
                if track_id(track) in ids
            ]

            unpublished = [
                track
                for track in current_tracks
                if track.get("acquisition", {}).get("status")
                != "published"
            ]

            if unpublished:
                names = [
                    track.get("metadata", {}).get("title", "?")
                    for track in unpublished
                ]
                raise RuntimeError(
                    "Release was not fully published: "
                    + ", ".join(names)
                )

            save_checkpoint(label)

            print(
                f"Release complete: {label}",
                flush=True,
            )

        except FatalStageError as exc:
            failed = True
            print(
                f"FATAL: {exc}",
                file=sys.stderr,
                flush=True,
            )
            print(
                "Stopping release processing so the remaining releases do not "
                "burn runner time with an invalid OpenAI credential.",
                file=sys.stderr,
                flush=True,
            )
            break

        except Exception as exc:
            failed = True
            print(
                f"RELEASE FAILED: {label}: {exc}",
                file=sys.stderr,
                flush=True,
            )

            failed_state = load_state()
            failed_tracks = [
                track
                for track in failed_state.get("tracks", [])
                if track_id(track) in ids
            ]

            # A failed search/download should not create a checkpoint because
            # its runner-local files disappear with the runner. However, if
            # publishing already succeeded for any track, preserve that
            # published state so the next run cannot upload it again.
            if any(
                track.get("acquisition", {}).get("status")
                == "published"
                for track in failed_tracks
            ):
                save_checkpoint(label + " (partial publish)")
            else:
                print(
                    "  Failed release state was not pushed; "
                    "the next run will retry this release from the "
                    "last successful checkpoint.",
                    flush=True,
                )

    print("", flush=True)
    print("=== Updating metadata playlists ===", flush=True)

    try:
        run_stage("scripts/playlists.py", os.environ.copy())

        # Persist playlist identity, snapshot IDs, configuration state, and
        # current playlist membership after the playlist stage succeeds.
        playlist_state = load_state()
        save_state(playlist_state)
        save_checkpoint("Playlist state")

    except Exception as exc:
        failed = True
        print(
            f"PLAYLIST UPDATE FAILED: {exc}",
            file=sys.stderr,
            flush=True,
        )

    if failed:
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
