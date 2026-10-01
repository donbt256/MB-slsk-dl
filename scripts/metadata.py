import json
import os
import subprocess
import time
import sys
import re
from difflib import SequenceMatcher
from collections import defaultdict
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
except ImportError:
    MutagenFile = EasyID3 = FLAC = MP4 = None

from publish import GitHubClient, normalize_path
from config import string


STATE_PATH = Path(string("paths.state_file", default="state/tracks.json"))
ONETAGGER = Path(os.environ.get("ONETAGGER_BIN", string("metadata.onetagger_binary", default="onetagger-cli")) )
CONFIG = Path(
    os.environ.get(
        "ONETAGGER_CONFIG",
        string("metadata.onetagger_config", default="onetagger/autotagger.json"),
    )
)

AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".alac",
    ".aiff",
    ".ape",
    ".wma",
}


def load_state():
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(state):
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(STATE_PATH)


def log(message=""):
    print(message, flush=True)


def run(command, cwd=None):
    log("$ " + " ".join(str(part) for part in command))
    return subprocess.run(command, cwd=cwd, check=False)


def local_repo_path(client, repo):
    path = client._local_repo(repo)
    client._run_git(["fetch", "origin"], cwd=path)
    return path


def normalize_match_text(value):
    value = str(value or "").casefold()
    value = re.sub(r"\\.(mp3|flac|m4a|aac|ogg|opus|wav|alac|aiff|ape|wma)$", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def track_number_from_entry(entry):
    metadata = entry.get("track", {}).get("metadata", {})
    value = (
        metadata.get("track_number")
        or metadata.get("track")
        or metadata.get("track_number_display")
        or ""
    )
    match = re.search(r"\\d+", str(value))
    return int(match.group()) if match else None


def expected_parts(entry):
    metadata = entry.get("track", {}).get("metadata", {})
    return {
        "artist": normalize_match_text(metadata.get("artist")),
        "album": normalize_match_text(metadata.get("album")),
        "title": normalize_match_text(
            metadata.get("title")
            or metadata.get("name")
        ),
        "track_number": track_number_from_entry(entry),
    }


def filename_parts(path):
    stem = path.stem
    match = re.match(r"^\\s*(\\d{1,3})(?:[ ._-]+)(.*)$", stem)
    number = int(match.group(1)) if match else None
    title = match.group(2) if match else stem

    parent = path.parent.name
    artist = path.parent.parent.name if path.parent.parent != path.parent else ""

    return {
        "artist": normalize_match_text(artist),
        "album": normalize_match_text(parent),
        "title": normalize_match_text(title),
        "track_number": number,
    }


def similarity(left, right):
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def fuzzy_match_track(entry, audio_files, used):
    expected = expected_parts(entry)
    candidates = []

    for path in audio_files:
        normalized = normalize_path(str(path))
        if normalized in used:
            continue

        actual = filename_parts(path)
        title_score = similarity(expected["title"], actual["title"])
        artist_score = similarity(expected["artist"], actual["artist"])
        album_score = similarity(expected["album"], actual["album"])

        if title_score < 0.72:
            continue

        score = (
            title_score * 0.60
            + artist_score * 0.20
            + album_score * 0.15
        )

        if (
            expected["track_number"] is not None
            and actual["track_number"] is not None
        ):
            if expected["track_number"] == actual["track_number"]:
                score += 0.05
            else:
                score -= 0.10

        candidates.append((score, title_score, path))

    if not candidates:
        return None, 0.0

    candidates.sort(
        key=lambda item: (item[0], item[1], str(item[2])),
        reverse=True,
    )
    best_score, _, best_path = candidates[0]

    # Require a strong overall match and a meaningful margin over the
    # runner-up so a similarly named track is not tagged accidentally.
    if best_score < 0.78:
        return None, best_score

    if len(candidates) > 1 and best_score - candidates[1][0] < 0.04:
        return None, best_score

    return best_path, best_score


def checkout_files(client, repo, branch):
    path = local_repo_path(client, repo)
    client._run_git(["checkout", "--force", branch], cwd=path)
    client._run_git(["reset", "--hard", f"origin/{branch}"], cwd=path)
    return path


def git_changed_files(client, repo_path):
    output = client._run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_path,
    ).decode("utf-8", errors="replace")

    changed = []

    for line in output.splitlines():
        if len(line) < 4:
            continue

        status = line[:2]
        value = line[3:].strip()

        if "->" in value:
            value = value.split("->", 1)[1].strip()

        value = value.strip('"')

        if (
            status == "??"
            or status[0] in {"M", "A"}
            or status[1] in {"M", "A"}
        ):
            changed.append(value)

    return changed


def find_failed_playlists(search_roots):
    found = []
    seen = set()
    for root in search_roots:
        if not root or not root.exists():
            continue
        try:
            iterator = root.rglob("failed-*.m3u")
        except OSError:
            continue
        for candidate in iterator:
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            key = str(resolved)
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            found.append(candidate)
    return sorted(found, key=lambda path: str(path))

def read_failed_playlist(path):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]

def seed_identifying_tags(file_path, entry):
    metadata = entry.get("track", {}).get("metadata", {})
    artist = str(metadata.get("artist") or "").strip()
    album = str(metadata.get("album") or "").strip()
    title = str(metadata.get("title") or metadata.get("name") or "").strip()
    album_artist = str(metadata.get("album_artist") or metadata.get("albumArtist") or artist).strip()
    track_number = track_number_from_entry(entry)

    if not any((artist, album, title, track_number)):
        return False
    if MutagenFile is None:
        raise RuntimeError("mutagen is required to seed identifying tags before OneTagger")

    suffix = file_path.suffix.lower()
    try:
        if suffix == ".mp3":
            try:
                audio = EasyID3(str(file_path))
            except Exception:
                audio = EasyID3()
            if artist:
                audio["artist"] = [artist]
            if album_artist:
                audio["albumartist"] = [album_artist]
            if album:
                audio["album"] = [album]
            if title:
                audio["title"] = [title]
            if track_number is not None:
                audio["tracknumber"] = [str(track_number)]
            audio.save(str(file_path))
        elif suffix == ".flac":
            audio = FLAC(str(file_path))
            if artist:
                audio["artist"] = [artist]
            if album_artist:
                audio["albumartist"] = [album_artist]
            if album:
                audio["album"] = [album]
            if title:
                audio["title"] = [title]
            if track_number is not None:
                audio["tracknumber"] = [str(track_number)]
            audio.save()
        elif suffix == ".m4a":
            audio = MP4(str(file_path))
            if artist:
                audio["\xa9ART"] = [artist]
            if album_artist:
                audio["aART"] = [album_artist]
            if album:
                audio["\xa9alb"] = [album]
            if title:
                audio["\xa9nam"] = [title]
            if track_number is not None:
                audio["trkn"] = [(track_number, 0)]
            audio.save()
        else:
            audio = MutagenFile(str(file_path), easy=True)
            if audio is None:
                return False
            if artist:
                audio["artist"] = [artist]
            if album_artist:
                audio["albumartist"] = [album_artist]
            if album:
                audio["album"] = [album]
            if title:
                audio["title"] = [title]
            if track_number is not None:
                audio["tracknumber"] = [str(track_number)]
            audio.save()
    except Exception as exc:
        raise RuntimeError(f"Failed to seed identifying tags in {file_path}: {exc}") from exc

    return True


def run_onetagger(path):
    command = [
        str(ONETAGGER),
        "autotagger",
        "--config",
        str(CONFIG.resolve()),
        "--path",
        str(path),
    ]

    log("$ " + " ".join(str(part) for part in command))
    log("  OneTagger output will be streamed live below.")

    before_playlists = set(find_failed_playlists([Path.home() / ".config" / "onetagger", Path.home() / ".local" / "share" / "onetagger", Path(os.environ.get("RUNNER_TEMP", "/tmp")), path]))

    process = subprocess.Popen(
        command,
        cwd=path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    started = time.monotonic()
    next_heartbeat = started + 30

    while True:
        line = process.stdout.readline()

        if line:
            print(line.rstrip("\n"), flush=True)
            continue

        return_code = process.poll()
        if return_code is not None:
            break

        now = time.monotonic()
        if now >= next_heartbeat:
            elapsed = int(now - started)
            minutes, seconds = divmod(elapsed, 60)
            log(
                f"  OneTagger still running... "
                f"elapsed {minutes:02d}:{seconds:02d}"
            )
            next_heartbeat = now + 30

        time.sleep(0.2)

    elapsed = int(time.monotonic() - started)
    minutes, seconds = divmod(elapsed, 60)
    log(
        f"  OneTagger process exited with code {return_code} "
        f"after {minutes:02d}:{seconds:02d}"
    )

    after_playlists = set(find_failed_playlists([Path.home() / ".config" / "onetagger", Path.home() / ".local" / "share" / "onetagger", Path(os.environ.get("RUNNER_TEMP", "/tmp")), path]))
    return return_code, sorted(after_playlists - before_playlists, key=lambda item: str(item))


def process_repo(client, repo, branch, track_entries):
    log("")
    log(f"=== Metadata: {repo} ===")
    log("Refreshing the complete library checkout for fuzzy metadata matching...")

    repo_path = checkout_files(client, repo, branch)

    audio_files = sorted(
        path
        for path in repo_path.rglob("*")
        if path.is_file()
        and path.suffix.lower() in AUDIO_EXTENSIONS
        and ".git" not in path.parts
    )

    log(f"Found {len(audio_files)} audio file(s) in {repo}.")

    matches = []
    used = set()
    unmatched = []

    for index, entry in enumerate(track_entries, 1):
        metadata = entry["track"].get("metadata", {})
        label = (
            f"{metadata.get('artist', '?')} - "
            f"{metadata.get('title', '?')}"
        )

        matched, score = fuzzy_match_track(
            entry,
            audio_files,
            used,
        )

        if matched is None:
            unmatched.append(entry)
            log(
                f"  [{index}/{len(track_entries)}] "
                f"UNMATCHED: {label} "
                f"(best score {score:.2f})"
            )
            continue

        normalized = normalize_path(str(matched.relative_to(repo_path)))
        used.add(normalized)
        matches.append((entry, matched))
        log(
            f"  [{index}/{len(track_entries)}] "
            f"MATCH {score:.2f}: {label} -> {normalized}"
        )

    if unmatched:
        log(
            f"Unmatched metadata entries: "
            f"{len(unmatched)}/{len(track_entries)}"
        )

    if not matches:
        log("No library files matched metadata entries.")
        return {
            "repo": repo,
            "changed": [],
            "success": True,
            "commit": None,
            "matched": {},
            "unmatched": unmatched,
        }

    directories = defaultdict(list)

    for entry, file_path in matches:
        directories[file_path.parent].append(file_path)

    failed = False
    failed_playlists = []
    failed_directories = []

    for directory, files in sorted(
        directories.items(),
        key=lambda item: str(item[0]),
    ):
        relative_directory = directory.relative_to(repo_path)

        log("")
        log(
            f"Tagging {len(files)} matched file(s) in "
            f"{relative_directory}"
        )

        for index, file_path in enumerate(files, 1):
            log(
                f"  [{index}/{len(files)}] "
                f"{file_path.relative_to(repo_path)}"
            )

        log("  Seeding identifying tags from acquisition metadata...")
        for file_path in files:
            seed_identifying_tags(file_path, next(entry for entry, matched_path in matches if matched_path == file_path))
        log("  Starting OneTagger...")
        return_code, new_playlists = run_onetagger(directory)
        failed_playlists.extend(new_playlists)

        if return_code == 0:
            log(
                f"  OneTagger completed successfully for "
                f"{relative_directory}"
            )
        else:
            log(
                f"  OneTagger exited with code {return_code} for "
                f"{relative_directory}"
            )
            failed = True
            failed_directories.append(str(relative_directory))

    changed = git_changed_files(client, repo_path)

    log("")
    log("Metadata changes detected:")
    if changed:
        for relative in changed:
            log(f"  changed: {relative}")
    else:
        log("  none")

    if not changed:
        log("No metadata changes were produced.")
        return {
            "repo": repo,
            "changed": [],
            "success": not failed,
            "commit": None,
            "matched": {
                normalize_path(str(path.relative_to(repo_path))): entry
                for entry, path in matches
            },
            "unmatched": unmatched,
            "failed_playlists": failed_playlists,
            "failed_directories": failed_directories,
        }

    entries = []
    changed_paths = []

    for relative in changed:
        normalized = normalize_path(relative)
        local = repo_path / normalized

        if not local.is_file():
            continue

        if (
            local.suffix.lower() not in AUDIO_EXTENSIONS
            and local.suffix.lower() != ".lrc"
        ):
            continue

        content = local.read_bytes()
        blob_sha = client.create_blob(repo, content)

        entries.append(
            {
                "path": normalized,
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )
        changed_paths.append(normalized)

    if not entries:
        return {
            "repo": repo,
            "changed": [],
            "success": not failed,
            "commit": None,
            "matched": {
                normalize_path(str(path.relative_to(repo_path))): entry
                for entry, path in matches
            },
            "unmatched": unmatched,
        }

    commit = client.create_tree_commit(
        repo=repo,
        branch=branch,
        entries=entries,
        message="Add OneTagger metadata",
    )

    log(
        f"Metadata committed: {repo} "
        f"({len(changed_paths)} file(s))"
    )

    return {
        "repo": repo,
        "changed": changed_paths,
        "success": not failed,
        "commit": commit,
        "matched": {
            normalize_path(str(path.relative_to(repo_path))): entry
            for entry, path in matches
        },
        "unmatched": unmatched,
        "failed_playlists": failed_playlists,
        "failed_directories": failed_directories,
    }


def write_failure_report(repo, result):
    report_dir = Path("state/metadata-failures")
    report_dir.mkdir(parents=True, exist_ok=True)
    safe_repo = re.sub(r"[^A-Za-z0-9._-]+", "_", repo)
    report_path = report_dir / (safe_repo + ".md")
    lines = [
        "# OneTagger failures: " + repo,
        "",
        "Generated: " + time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "",
        "Status: " + ("OK" if result["success"] else "FAILED"),
        "Matched state entries: " + str(len(result.get("matched", {}))),
        "Unmatched state entries: " + str(len(result.get("unmatched", []))),
        "",
        "## Failed songs",
        "",
    ]
    seen = set()
    playlists = result.get("failed_playlists", [])
    for playlist in playlists:
        lines.append("### " + playlist.name)
        for song in read_failed_playlist(playlist):
            if song not in seen:
                seen.add(song)
                lines.append("- `" + song + "`")
        lines.append("")
    if not playlists:
        lines.append("No OneTagger failed-song playlist was produced.")
        lines.append("")
    if result.get("failed_directories"):
        lines += ["## Directories with OneTagger errors", ""]
        lines += ["- `" + value + "`" for value in result["failed_directories"]]
        lines.append("")
    if result.get("unmatched"):
        lines += ["## Metadata entries not matched to a library file", ""]
        for entry in result["unmatched"]:
            metadata = entry.get("track", {}).get("metadata", {})
            lines.append("- " + str(metadata.get("artist", "?")) + " - " + str(metadata.get("title", "?")))
        lines.append("")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def main():
    if not ONETAGGER.is_file():
        raise RuntimeError(
            f"OneTagger binary not found: {ONETAGGER}"
        )

    if not CONFIG.is_file():
        raise RuntimeError(
            f"OneTagger config not found: {CONFIG}"
        )

    state = load_state()

    entries_by_repo = defaultdict(list)

    for track in state.get("tracks", []):
        acquisition = track.get("acquisition", {})
        if not isinstance(acquisition, dict):
            continue

        if acquisition.get("status") != "published":
            continue

        enrichment = track.get("enrichment", {})
        onetagger_state = (
            enrichment.get("onetagger", {})
            if isinstance(enrichment, dict)
            else {}
        )
        if (
            isinstance(onetagger_state, dict)
            and onetagger_state.get("status") == "committed"
        ):
            continue

        library = acquisition.get("library")
        if not isinstance(library, dict):
            continue

        repo = str(library.get("repo") or "").strip()
        path = normalize_path(library.get("path") or "")

        if not repo or not path:
            continue

        entries_by_repo[repo].append(
            {
                "track": track,
                "path": path,
            }
        )

    if not entries_by_repo:
        log("No published music is available for metadata processing.")
        return 0

    client = GitHubClient(os.environ.get("GIT_PAT"))

    overall_success = True
    committed_by_repo = {}

    for repo, entries in sorted(entries_by_repo.items()):
        repo_state = client.get_repo_state(repo)

        result = process_repo(
            client,
            repo,
            repo_state["branch"],
            entries,
        )

        committed_by_repo[repo] = result
        result["failure_report"] = str(write_failure_report(repo, result))

        if not result["success"]:
            overall_success = False

        changed = set(result["changed"])
        matched_by_state_path = {
            id(entry["track"]): actual_path
            for actual_path, entry in result.get("matched", {}).items()
        }

        for entry in entries:
            track = entry["track"]
            state_path = entry["path"]
            path = matched_by_state_path.get(id(track), state_path)
            enrichment = track.setdefault(
                "enrichment",
                {
                    "artwork": None,
                    "lyrics": None,
                },
            )

            metadata_status = enrichment.setdefault(
                "onetagger",
                {},
            )

            if path in changed:
                metadata_status["status"] = "committed"
                metadata_status["repository"] = repo
                metadata_status["path"] = path
                metadata_status["commit"] = result["commit"]
            else:
                metadata_status["status"] = (
                    "unchanged" if result["success"] else "failed"
                )

    save_state(state)

    # Persist metadata completion in the source repository so a later
    # workflow run can retry only tracks that were not successfully enriched.
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
    subprocess.run(["git", "add", str(STATE_PATH), "state/metadata-failures"], check=True)

    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        check=False,
    )

    if staged.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "Checkpoint metadata state"],
            check=True,
        )
        subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            check=True,
        )

    log("")
    log("=== Metadata summary ===")

    for repo, result in committed_by_repo.items():
        log(
            f"{repo}: "
            f"{len(result['changed'])} file(s) changed, "
            f"commit={result['commit'] or 'none'}, "
            f"status={'ok' if result['success'] else 'failed'}, " f"failed_songs={sum(len(read_failed_playlist(p)) for p in result.get('failed_playlists', []))}, " f"report={result.get('failure_report', 'none')}"
        )

    if overall_success:
        log("Metadata stage complete.")
        return 0

    log(
        "Metadata stage completed with failures. "
        "Music commits were already pushed and are unaffected."
    )
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nMetadata stage interrupted.", file=sys.stderr)
        raise
