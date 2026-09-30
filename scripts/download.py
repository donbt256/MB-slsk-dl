import json
import os
import sys
import time
from pathlib import Path


from soulseek import SoulseekClient
from release import release_key as metadata_release_key
from matcher import has_audio_extension


RELEASE_FILTER = os.environ.get("RELEASE_KEY")


STATE_FILE = Path("state/tracks.json")

DOWNLOAD_ROOT = Path(
    os.environ.get(
        "SOULSEEK_DOWNLOAD_DIR",
        "/home/runner/music-downloads",
    )
)

POLL_SECONDS = 5
TIMEOUT_SECONDS = 60 * 60

ZERO_SPEED_SECONDS = 30

# A user is considered consistently slow after this many completed
# transfers whose observed transfer speed stays at or below the threshold.
SLOW_USER_SPEED_BYTES = 100 * 1024
SLOW_USER_STRIKES = 3

# Exact remote files that repeatedly fail should be suppressed even when
# the Soulseek user is otherwise usable. "File not shared" is immediately
# deterministic; timeout/cancellation failures require two occurrences.
TRANSFER_FAILURE_STRIKES = 2

FAILURE_STATES = (
    "rejected",
    "timedout",
    "errored",
    "failed",
    "cancelled",
    "canceled",
)


def log(message=""):
    print(message, flush=True)


def load_state():
    log("Loading acquisition state...")

    with STATE_FILE.open(
        "r",
        encoding="utf-8",
    ) as handle:
        state = json.load(handle)

    log(
        f"Loaded {len(state.get('tracks', []))} tracks."
    )

    return state


def save_state(state):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = STATE_FILE.with_suffix(
        ".tmp"
    )

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

    temporary.replace(STATE_FILE)


def normalize_username(username):
    return str(username or "").strip().casefold()


def slow_user_state(state):
    users = state.setdefault("slow_users", {})
    if not isinstance(users, dict):
        state["slow_users"] = {}
        users = state["slow_users"]
    return users


def is_user_blacklisted(state, username):
    key = normalize_username(username)
    if not key:
        return False

    entry = slow_user_state(state).get(key)
    return isinstance(entry, dict) and entry.get("blacklisted") is True


def record_user_speed(state, username, speed):
    """Record one completed transfer and blacklist persistently slow users."""
    key = normalize_username(username)
    if not key:
        return False

    try:
        speed = float(speed or 0)
    except (TypeError, ValueError):
        return False

    # A missing/zero speed measurement is not evidence that the user is
    # slow. Only measured positive speeds can accumulate slow-user strikes.
    if speed <= 0:
        return False

    users = slow_user_state(state)
    entry = users.setdefault(
        key,
        {
            "username": str(username),
            "slow_transfers": 0,
            "blacklisted": False,
        },
    )

    if entry.get("blacklisted") is True:
        return True

    entry["last_speed"] = speed
    entry["last_speed_timestamp"] = int(time.time())

    if speed <= SLOW_USER_SPEED_BYTES:
        entry["slow_transfers"] = int(
            entry.get("slow_transfers", 0)
        ) + 1
    else:
        entry["slow_transfers"] = 0

    if entry["slow_transfers"] >= SLOW_USER_STRIKES:
        entry["blacklisted"] = True
        log(
            f"  Blacklisting Soulseek user {username}: "
            f"{entry['slow_transfers']} consecutive completed "
            f"transfers at or below "
            f"{format_speed(SLOW_USER_SPEED_BYTES)}."
        )
        return True

    return False


def transfer_key(username, filename):
    return (
        str(username or "").lower(),
        str(filename or "").lower(),
    )


def transfer_failure_key(username, filename):
    return (
        f"{normalize_username(username)}\x1f"
        f"{str(filename or '').casefold()}"
    )


def transfer_failure_state(state):
    failures = state.setdefault("failed_transfers", {})
    if not isinstance(failures, dict):
        state["failed_transfers"] = {}
        failures = state["failed_transfers"]
    return failures


def is_transfer_blacklisted(state, username, filename):
    key = transfer_failure_key(username, filename)
    entry = transfer_failure_state(state).get(key)
    return isinstance(entry, dict) and entry.get("blacklisted") is True


def record_transfer_failure(state, username, filename, error):
    """Suppress a remote file after deterministic/repeated transfer failures."""
    if not username or not filename:
        return False

    key = transfer_failure_key(username, filename)
    failures = transfer_failure_state(state)
    entry = failures.setdefault(
        key,
        {
            "username": str(username),
            "filename": str(filename),
            "strikes": 0,
            "blacklisted": False,
        },
    )

    if entry.get("blacklisted") is True:
        return True

    message = str(error or "").strip()
    entry["last_error"] = message
    entry["last_error_timestamp"] = int(time.time())

    deterministic = "file not shared" in message.casefold()
    if deterministic:
        entry["blacklisted"] = True
    else:
        entry["strikes"] = int(entry.get("strikes", 0)) + 1
        if entry["strikes"] >= TRANSFER_FAILURE_STRIKES:
            entry["blacklisted"] = True

    if entry["blacklisted"]:
        log(
            f"  Suppressing failed Soulseek file: "
            f"{username} / {filename} "
            f"({message or 'transfer failure'})."
        )
        return True

    return False


def selected_matches(state):
    matches = []

    for track in state.get("tracks", []):
        acquisition = track.get(
            "acquisition",
            {},
        )

        if not isinstance(acquisition, dict):
            continue

        if acquisition.get("status") != "matched":
            continue

        match = acquisition.get("match")

        if not isinstance(match, dict):
            continue

        candidates = match.get(
            "candidates"
        )

        if (
            not isinstance(candidates, list)
            or not candidates
        ):
            continue

        selected = candidates[0]

        if not isinstance(selected, dict):
            continue

        username = selected.get(
            "username"
        )

        filename = selected.get(
            "filename"
        )

        if not username or not filename or not has_audio_extension(filename):
            continue

        matches.append(
            (
                track,
                selected,
            )
        )

    return matches


def album_release_candidates(tracks):
    """
    Return release candidates for an album group.

    The search stage stores the same ranked release list
    on every track in the album group. We only need the
    first track because each release contains its complete
    track mapping.
    """

    if not tracks:
        return []

    search = tracks[0].get(
        "search",
        {},
    )

    releases = search.get(
        "release_candidates",
        [],
    )

    # The release candidates are also retained in the deterministic
    # matcher state. This matters across checkpoints: search.py may have
    # skipped a track because it was previously downloaded, while
    # compaction can retain the matcher candidates as the durable source.
    if not isinstance(releases, list) or not releases:
        matching = tracks[0].get(
            "matching",
            {},
        )

        deterministic = matching.get(
            "deterministic",
            {},
        )

        releases = deterministic.get(
            "candidates",
            [],
        )

    if not isinstance(releases, list):
        return []

    return [
        release
        for release in releases
        if isinstance(release, dict)
    ]


def release_candidate_key(release):
    return (
        str(
            release.get("username")
            or ""
        ).lower(),
        str(
            release.get("folder")
            or ""
        ).lower(),
    )


def release_matches(release, tracks, state=None):
    """
    Convert a ranked release's match list into:

        [(track, candidate), ...]

    Only tracks actually mapped by the release are returned.
    """

    by_track_id = {
        str(
            track.get("metadata", {}).get(
                "id"
            )
        ): track
        for track in tracks
    }

    matches = []

    for item in release.get(
        "matches",
        [],
    ):
        if not isinstance(item, dict):
            continue

        track_id = str(
            item.get("track_id")
        )

        track = by_track_id.get(
            track_id
        )

        candidate = item.get(
            "candidate"
        )

        if track is None:
            continue

        if not isinstance(
            candidate,
            dict,
        ):
            continue

        username = candidate.get(
            "username"
        )

        filename = candidate.get(
            "filename"
        )

        if not username or not filename:
            continue

        if (
            state is not None
            and is_transfer_blacklisted(
                state,
                username,
                filename,
            )
        ):
            continue

        matches.append(
            (
                track,
                candidate,
            )
        )

    return matches


def extract_transfers(data):
    transfers = []

    if not isinstance(data, list):
        return transfers

    for user_entry in data:
        if not isinstance(
            user_entry,
            dict,
        ):
            continue

        username = user_entry.get(
            "username"
        )

        directories = user_entry.get(
            "directories",
            [],
        )

        if not isinstance(
            directories,
            list,
        ):
            continue

        for directory in directories:
            if not isinstance(
                directory,
                dict,
            ):
                continue

            files = directory.get(
                "files",
                [],
            )

            if not isinstance(
                files,
                list,
            ):
                continue

            for transfer in files:
                if not isinstance(
                    transfer,
                    dict,
                ):
                    continue

                item = dict(transfer)

                item.setdefault(
                    "username",
                    username,
                )

                transfers.append(item)

    return transfers


def transfer_filename(transfer):
    return (
        transfer.get("filename")
        or transfer.get("fileName")
        or transfer.get("remoteFilename")
    )


def transfer_local_filename(transfer):
    return (
        transfer.get("localFilename")
        or transfer.get("localFileName")
        or transfer.get("local_filename")
    )


def transfer_username(transfer):
    return (
        transfer.get("username")
        or transfer.get("userName")
    )


def transfer_id(transfer):
    return (
        transfer.get("id")
        or transfer.get("transferId")
        or transfer.get("transferID")
    )


def transfer_state(transfer):
    state = transfer.get("state")

    if isinstance(state, dict):
        return str(
            state.get("description")
            or state.get("state")
            or state.get("name")
            or ""
        )

    return str(
        state or ""
    )


def transfer_progress(transfer):
    total = (
        transfer.get("size")
        or transfer.get("fileSize")
        or transfer.get("totalBytes")
        or transfer.get("totalSize")
    )

    downloaded = (
        transfer.get("bytesTransferred")
        or transfer.get("bytesDownloaded")
        or transfer.get("transferred")
        or transfer.get("downloaded")
        or 0
    )

    speed = (
        transfer.get("averageSpeed")
        or transfer.get("speed")
        or transfer.get("downloadSpeed")
        or 0
    )

    try:
        total = int(
            total or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        total = 0

    try:
        downloaded = int(
            downloaded or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        downloaded = 0

    try:
        speed = float(
            speed or 0
        )
    except (
        TypeError,
        ValueError,
    ):
        speed = 0

    return (
        total,
        downloaded,
        speed,
    )


def format_bytes(value):
    try:
        value = float(value)
    except (
        TypeError,
        ValueError,
    ):
        return "?"

    units = (
        "B",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
    )

    for unit in units:
        if abs(value) < 1024:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} PiB"


def format_speed(value):
    if not value:
        return "?"

    return (
        f"{format_bytes(value)}/s"
    )


def normalize_remote_filename(
    filename,
):
    return str(
        filename or ""
    ).replace(
        "\\",
        "/",
    )


def remote_basename(filename):
    normalized = normalize_remote_filename(
        filename
    )

    if not normalized:
        return ""

    return Path(
        normalized
    ).name


def find_downloaded_file(
    download_dir,
    remote_filename,
    expected_size=None,
    known_files=None,
):
    """
    Find a completed download under the configured
    slskd download directory.

    Soulseek remote paths are not valid local Linux
    paths, so the remote path is never reconstructed
    directly.

    Preference order:

    1. A newly-created file with the exact basename
       and expected size.
    2. Any exact-basename file with expected size.
    3. Any exact-basename file.

    known_files is an optional set of paths that existed
    before the transfer was queued. This prevents an
    unrelated pre-existing file with the same basename
    from being selected when a newer file is available.
    """

    root = Path(
        download_dir
    )

    if not root.exists():
        return None

    basename = remote_basename(
        remote_filename
    )

    if not basename:
        return None

    if known_files is None:
        known_files = set()

    exact = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if path.name != basename:
            continue

        exact.append(path)

    if not exact:
        return None

    new_files = [
        path
        for path in exact
        if path not in known_files
    ]

    size_matches = []

    if expected_size is not None:
        try:
            expected_size = int(
                expected_size
            )
        except (
            TypeError,
            ValueError,
        ):
            expected_size = None

    if expected_size is not None:
        for path in new_files:
            try:
                if (
                    path.stat().st_size
                    == expected_size
                ):
                    size_matches.append(
                        path
                    )
            except OSError:
                pass

        if size_matches:
            return sorted(
                size_matches,
                key=lambda path: str(path),
            )[0]

        for path in exact:
            try:
                if (
                    path.stat().st_size
                    == expected_size
                ):
                    size_matches.append(
                        path
                    )
            except OSError:
                pass

        if size_matches:
            return sorted(
                size_matches,
                key=lambda path: str(path),
            )[0]

    if new_files:
        return sorted(
            new_files,
            key=lambda path: str(path),
        )[0]

    return sorted(
        exact,
        key=lambda path: str(path),
    )[0]


def repair_stale_download_states(state, tracks):
    """
    Runner-local downloads do not survive a new GitHub Actions runner.

    A checkpoint can legitimately contain a 'downloaded' state from a
    previous runner, but its local file will no longer exist. Reset those
    tracks to 'matched' so the current run reacquires them instead of
    sending a stale path to the publisher.
    """
    changed = False

    for track in tracks:
        acquisition = track.setdefault("acquisition", {})
        status = acquisition.get("status")

        if status not in {"downloaded", "ready_to_publish"}:
            continue

        file_info = acquisition.get("file")
        path = None
        if isinstance(file_info, dict):
            path = (
                file_info.get("path")
                or file_info.get("local_path")
                or file_info.get("localPath")
            )

        if path and Path(path).is_file():
            continue

        if isinstance(acquisition.get("match"), dict) and acquisition["match"].get("candidates"):
            acquisition["status"] = "matched"
            acquisition.pop("file", None)
            acquisition.pop("download_error", None)
            changed = True
            log(
                f"  Stale download reset: "
                f"{track.get('metadata', {}).get('title', '?')} "
                "will be reacquired on this runner."
            )
        else:
            acquisition["status"] = "download_failed"
            acquisition["download_error"] = (
                "Downloaded state exists but the local file is missing "
                "and no retained match is available for reacquisition."
            )
            changed = True
            log(
                f"  Missing downloaded file with no match: "
                f"{track.get('metadata', {}).get('title', '?')}"
            )

    if changed:
        save_state(state)

    return changed


def snapshot_download_files():
    if not DOWNLOAD_ROOT.exists():
        return set()

    return {
        path
        for path in DOWNLOAD_ROOT.rglob("*")
        if path.is_file()
    }


def cancel_transfer(
    client,
    transfer,
):
    """
    Best-effort cancellation of one slskd download.

    slskd exposes DELETE /api/v0/transfers/downloads/{username}/{id}.
    The existing SoulseekClient owns the requests session, so use
    that session when available rather than requiring another dependency.
    """

    username = transfer_username(
        transfer
    )

    identifier = transfer_id(
        transfer
    )

    if not username or identifier is None:
        return False

    session = getattr(
        client,
        "session",
        None,
    )

    base_url = getattr(
        client,
        "base_url",
        None,
    )

    if session is None or not base_url:
        return False

    url = (
        f"{base_url.rstrip('/')}"
        f"/api/v0/transfers/downloads/"
        f"{username}/{identifier}"
    )

    try:
        response = session.delete(
            url,
            timeout=30,
        )

        if response.status_code in {
            200,
            202,
            204,
            404,
        }:
            return True

        log(
            f"  Cancel returned HTTP "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )

    except Exception as exc:
        log(
            f"  Unable to cancel transfer: "
            f"{exc}"
        )

    return False


def cancel_release_transfers(
    client,
    transfer_map,
):
    if not transfer_map:
        return

    log(
        f"  Cancelling {len(transfer_map)} "
        "remaining transfer(s)..."
    )

    for transfer in transfer_map.values():
        cancel_transfer(
            client,
            transfer,
        )


def mark_release_attempt(
    tracks,
    release,
    attempt_number,
    status,
):
    for track in tracks:
        acquisition = track.setdefault(
            "acquisition",
            {},
        )

        attempts = acquisition.setdefault(
            "download_attempts",
            [],
        )

        attempts.append(
            {
                "attempt": attempt_number,
                "release_id": release.get(
                    "release_id"
                ),
                "username": release.get(
                    "username"
                ),
                "folder": release.get(
                    "folder"
                ),
                "score": release.get(
                    "score"
                ),
                "status": status,
                "timestamp": int(
                    time.time()
                ),
            }
        )


def set_release_match(
    tracks,
    release,
):
    matches = release_matches(
        release,
        tracks,
    )

    by_track_id = {
        str(
            track.get("metadata", {}).get(
                "id"
            )
        ): candidate
        for track, candidate in matches
    }

    for track in tracks:
        track_id = str(
            track.get("metadata", {}).get(
                "id"
            )
        )

        candidate = by_track_id.get(
            track_id
        )

        if candidate is None:
            continue

        track.setdefault(
            "acquisition",
            {},
        )["match"] = {
            "candidates": [
                candidate
            ],
        }


def album_groups_from_state(
    state,
):
    groups = {}

    for track in state.get(
        "tracks",
        [],
    ):
        metadata = track.get(
            "metadata",
            {},
        )

        artist = str(
            metadata.get(
                "album_artist"
            )
            or metadata.get(
                "artist"
            )
            or ""
        ).strip()

        album = str(
            metadata.get(
                "album"
            )
            or ""
        ).strip()

        if not artist or not album:
            continue

        key = (
            artist.casefold(),
            album.casefold(),
        )

        groups.setdefault(
            key,
            [],
        ).append(track)

    return groups


def queue_release(
    client,
    matches,
):
    """
    Queue a bounded number of files from one selected release.

    slskd waits for the remote peer to acknowledge each enqueue.
    Keeping only a small number of transfers active prevents a single
    Soulseek peer from being flooded with a whole album at once.
    """
    queued = []

    for index, (track, candidate) in enumerate(
        matches,
        start=1,
    ):
        title = track.get(
            "metadata",
            {},
        ).get(
            "title",
            "?",
        )

        username = candidate.get("username")
        filename = candidate.get("filename")
        size = candidate.get("size")

        log(
            f"  [{index}/{len(matches)}] "
            f"Queueing: {title}"
        )
        log(f"    User: {username}")
        log(f"    File: {filename}")

        if size is not None:
            log(
                f"    Size: {format_bytes(size)}"
            )

        try:
            client.enqueue_download(
                username=username,
                filename=filename,
                size=size,
            )
            queued.append((track, candidate))
        except Exception as exc:
            log(f"    FAILED: {exc}")
            break

    return queued


def release_download(
    client,
    state,
    tracks,
    release,
    attempt_number,
    fallback_candidates=None,
):
    """
    Attempt one complete album release.

    Only a bounded number of transfers are remotely queued at once.
    New files are enqueued as earlier files finish. This avoids
    saturating a Soulseek peer's request/queue handling while retaining
    concurrent downloads.

    The release succeeds only when every requested track has a
    completed local file.
    """

    username = release.get("username")
    folder = release.get("folder")
    release_id = release.get("release_id")

    log("")
    log(f"Trying release {attempt_number}:")
    log(f"  User: {username}")
    log(f"  Folder: {folder}")
    log(f"  Release ID: {release_id}")
    log(f"  Score: {release.get('score')}")
    log(
        f"  Coverage: {release.get('matched_tracks')}/"
        f"{release.get('expected_tracks')}"
    )

    matches = release_matches(
        release,
        tracks,
        state,
    )

    if len(matches) != len(tracks):
        log(
            "  Release does not contain "
            "a candidate for every requested track."
        )
        return False

    known_files = snapshot_download_files()

    max_active = max(
        1,
        int(
            os.environ.get(
                "SLSKD_MAX_ACTIVE_RELEASE_DOWNLOADS",
                "4",
            )
        ),
    )

    pending = list(matches)
    wanted = {}
    completed = set()
    failed = set()
    transfers_seen = {}

    # Candidates from other already-accepted release mappings can replace
    # an individual file that fails during this release. Keep the original
    # release mapping first, then try alternatives only when necessary.
    fallback_candidates = fallback_candidates or {}
    attempted_candidates = {
        str(track.get("metadata", {}).get("id")): {
            transfer_key(candidate.get("username"), candidate.get("filename"))
        }
        for track, candidate in matches
    }

    release_started = time.monotonic()
    last_nonzero_speed = release_started
    deadline = release_started + TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        # Fill the bounded active queue. A transfer remains active until
        # slskd reports it completed or failed.
        while (
            pending
            and len(wanted) - len(completed) - len(failed)
            < max_active
        ):
            track, candidate = pending.pop(0)

            candidate_username = candidate.get("username")

            if is_user_blacklisted(
                state,
                candidate_username,
            ):
                log(
                    f"  Skipping newly blacklisted user: "
                    f"{candidate_username}"
                )
                failed.add(
                    transfer_key(
                        candidate_username,
                        candidate.get("filename"),
                    )
                )
                continue

            queued = queue_release(
                client,
                [(track, candidate)],
            )

            if not queued:
                log(
                    "  Could not queue the next release file."
                )
                failed.add(
                    transfer_key(
                        candidate.get("username"),
                        candidate.get("filename"),
                    )
                )
                break

            queued_track, queued_candidate = queued[0]
            key = transfer_key(
                queued_candidate.get("username"),
                queued_candidate.get("filename"),
            )
            wanted[key] = (
                queued_track,
                queued_candidate,
            )

        if failed:
            # Replace failed files with the next deterministic candidate
            # before abandoning the whole release.
            replacements = []
            unrecoverable = []

            for failed_key in list(failed):
                failed_item = wanted.get(failed_key)
                if failed_item is None:
                    unrecoverable.append(failed_key)
                    continue

                failed_track, failed_candidate = failed_item
                track_id = str(
                    failed_track.get("metadata", {}).get("id")
                )

                replacement = None
                for candidate in fallback_candidates.get(track_id, []):
                    if not isinstance(candidate, dict):
                        continue

                    username = candidate.get("username")
                    filename = candidate.get("filename")

                    if not username or not filename:
                        continue

                    candidate_key = transfer_key(
                        username,
                        filename,
                    )

                    if candidate_key in attempted_candidates.setdefault(
                        track_id,
                        set(),
                    ):
                        continue

                    attempted_candidates[track_id].add(candidate_key)

                    if is_user_blacklisted(state, username):
                        continue

                    if is_transfer_blacklisted(
                        state,
                        username,
                        filename,
                    ):
                        continue

                    replacement = candidate
                    break

                if replacement is None:
                    unrecoverable.append(failed_key)
                    continue

                replacements.append(
                    (
                        failed_key,
                        failed_track,
                        replacement,
                    )
                )

            for failed_key, failed_track, replacement in replacements:
                failed.discard(failed_key)
                wanted.pop(failed_key, None)

                queued = queue_release(
                    client,
                    [(failed_track, replacement)],
                )

                if not queued:
                    log(
                        "    Replacement candidate could not be queued."
                    )
                    failed.add(failed_key)
                    continue

                replacement_track, replacement_candidate = queued[0]
                replacement_key = transfer_key(
                    replacement_candidate.get("username"),
                    replacement_candidate.get("filename"),
                )
                wanted[replacement_key] = (
                    replacement_track,
                    replacement_candidate,
                )

                log(
                    f"    Retrying track with alternate candidate: "
                    f"{replacement_candidate.get('username')} / "
                    f"{replacement_candidate.get('filename')}"
                )

            failed.update(unrecoverable)

            if failed:
                log(
                    f"  Release failed: "
                    f"{len(failed)} transfer(s) have no usable "
                    "replacement candidate."
                )
                cancel_release_transfers(
                    client,
                    transfers_seen,
                )
                mark_release_attempt(
                    tracks,
                    release,
                    attempt_number,
                    "failed",
                )
                save_state(state)
                return False

        if not wanted and not pending:
            log("  Release has no files to download.")
            return False

        try:
            data = client.get_downloads()
        except Exception as exc:
            log(
                f"  Unable to read download status: {exc}"
            )
            time.sleep(POLL_SECONDS)
            continue

        transfers = extract_transfers(data)

        for transfer in transfers:
            username_now = transfer_username(transfer)
            filename_now = transfer_filename(transfer)

            if not username_now or not filename_now:
                continue

            key = transfer_key(
                username_now,
                filename_now,
            )

            if key not in wanted:
                continue

            if key in completed or key in failed:
                continue

            transfers_seen[key] = transfer
            current_state = transfer_state(transfer).lower()
            total, downloaded, speed = transfer_progress(transfer)

            if total:
                percent = downloaded / total * 100
                log(f"  Transfer: {filename_now}")
                log(f"    State: {current_state or 'unknown'}")
                log(
                    f"    Progress: {format_bytes(downloaded)} / "
                    f"{format_bytes(total)} ({percent:.1f}%)"
                )
                log(f"    Speed: {format_speed(speed)}")
            else:
                log(f"  Transfer: {filename_now}")
                log(f"    State: {current_state or 'unknown'}")
                log(f"    Progress: {format_bytes(downloaded)}")
                log(f"    Speed: {format_speed(speed)}")

            if speed > 0:
                last_nonzero_speed = time.monotonic()

            if "succeeded" in current_state:
                track, candidate = wanted[key]
                expected_size = candidate.get("size")

                local_filename = transfer_local_filename(
                    transfer
                )

                path = None

                if local_filename:
                    local_path = Path(local_filename)

                    if (
                        local_path.is_file()
                        and (
                            expected_size is None
                            or local_path.stat().st_size
                            == int(expected_size)
                        )
                    ):
                        path = local_path

                if path is None:
                    path = find_downloaded_file(
                        DOWNLOAD_ROOT,
                        filename_now,
                        expected_size=expected_size,
                        known_files=known_files,
                    )

                if path is None:
                    log(
                        "    Transfer succeeded, "
                        "but the downloaded file was not found yet."
                    )
                    continue

                actual_size = path.stat().st_size

                if (
                    expected_size is not None
                    and actual_size != int(expected_size)
                ):
                    log(
                        f"    File exists but size does not match "
                        f"expected size: {format_bytes(actual_size)} "
                        f"vs {format_bytes(expected_size)}"
                    )
                    continue

                track.setdefault(
                    "acquisition",
                    {},
                )["file"] = {
                    "path": str(path),
                    "filename": path.name,
                    "size": actual_size,
                }

                completed.add(key)

                blacklisted = record_user_speed(
                    state,
                    username_now,
                    speed,
                )

                if blacklisted:
                    log(
                        f"    User {username_now} is now blacklisted; "
                        "cancelling its remaining active transfers."
                    )

                    for active_key, active_transfer in list(
                        transfers_seen.items()
                    ):
                        if (
                            active_key[0]
                            == normalize_username(username_now)
                            and active_key not in completed
                            and active_key not in failed
                        ):
                            cancel_transfer(
                                client,
                                active_transfer,
                            )
                            failed.add(active_key)

                log(f"    Downloaded: {path}")

            elif any(
                failure in current_state
                for failure in FAILURE_STATES
            ):
                log(f"    Download failed: {current_state}")
                record_transfer_failure(
                    state,
                    username_now,
                    filename_now,
                    current_state,
                )
                failed.add(key)

        if failed:
            log(
                f"  Release failed: "
                f"{len(failed)} transfer(s) failed."
            )
            cancel_release_transfers(
                client,
                transfers_seen,
            )
            mark_release_attempt(
                tracks,
                release,
                attempt_number,
                "failed",
            )
            save_state(state)
            return False

        if (
            len(completed) == len(matches)
            and not pending
        ):
            for track, candidate in matches:
                track.setdefault(
                    "acquisition",
                    {},
                )["status"] = "downloaded"

            mark_release_attempt(
                tracks,
                release,
                attempt_number,
                "succeeded",
            )
            save_state(state)

            log(
                f"  Release succeeded: "
                f"{len(completed)}/{len(matches)} files"
            )
            return True

        now = time.monotonic()

        if (
            now - last_nonzero_speed
            >= ZERO_SPEED_SECONDS
        ):
            log(
                f"  Release has had zero transfer speed for "
                f"{ZERO_SPEED_SECONDS} seconds."
            )
            cancel_release_transfers(
                client,
                transfers_seen,
            )
            mark_release_attempt(
                tracks,
                release,
                attempt_number,
                "zero_speed",
            )
            save_state(state)
            return False

        log(
            f"  Release progress: "
            f"{len(completed)}/{len(matches)} downloaded; "
            f"{len(pending)} pending; "
            f"{len(wanted) - len(completed) - len(failed)} active"
        )

        time.sleep(POLL_SECONDS)

    log("  Release timed out.")
    cancel_release_transfers(
        client,
        transfers_seen,
    )
    mark_release_attempt(
        tracks,
        release,
        attempt_number,
        "timeout",
    )
    save_state(state)
    return False


def process_album(
    client,
    state,
    tracks,
):
    if not tracks:
        return True

    first = tracks[0].get(
        "metadata",
        {},
    )

    artist = (
        first.get(
            "album_artist"
        )
        or first.get(
            "artist"
        )
        or ""
    )

    album = first.get(
        "album",
        "",
    )

    releases = (
        album_release_candidates(
            tracks
        )
    )

    if not releases:
        log(
            f"No release candidates stored "
            f"for {artist} - {album}"
        )

        return False

    # The release candidates are ranked by matcher.py.
    #
    # Do not skip a release merely because a previous runner recorded a
    # successful transfer. GitHub Actions runners are ephemeral, so the
    # downloaded files may no longer exist even though the checkpointed
    # attempt says "succeeded". A fresh run must be allowed to reacquire
    # the release.
    log("")
    log(
        f"Album: {artist} - {album}"
    )

    log(
        f"  Stored release candidates: "
        f"{len(releases)}"
    )

    attempt_number = 0

    for release in releases:
        release_username = release.get("username")

        if is_user_blacklisted(state, release_username):
            log(
                f"  Skipping blacklisted user: {release_username}"
            )
            continue

        release_id = release.get(
            "release_id"
        )

        attempt_number += 1

        fallback_by_track = {}
        for alternate_release in releases:
            for item in alternate_release.get("matches", []):
                if not isinstance(item, dict):
                    continue
                track_id = str(item.get("track_id"))
                candidate = item.get("candidate")
                if (
                    track_id
                    and isinstance(candidate, dict)
                    and candidate.get("username")
                    and candidate.get("filename")
                ):
                    fallback_by_track.setdefault(track_id, []).append(candidate)

        success = release_download(
            client,
            state,
            tracks,
            release,
            attempt_number,
            fallback_by_track,
        )

        if success:
            return True

        log(
            "  Release did not complete. "
            "Trying next release candidate."
        )

    log(
        f"  No complete release succeeded "
        f"for {artist} - {album}"
    )

    for track in tracks:
        acquisition = track.setdefault(
            "acquisition",
            {},
        )

        if acquisition.get(
            "status"
        ) != "downloaded":
            acquisition["status"] = (
                "download_failed"
            )

    save_state(state)

    return False


def process_individual_track(
    client,
    state,
    track,
):
    acquisition = track.setdefault(
        "acquisition",
        {},
    )

    match = acquisition.get("match")

    if not isinstance(match, dict):
        return False

    candidates = match.get("candidates")

    if not isinstance(candidates, list) or not candidates:
        return False

    title = track.get(
        "metadata",
        {},
    ).get(
        "title",
        "?",
    )

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        username = candidate.get("username")
        filename = candidate.get("filename")
        size = candidate.get("size")

        if not username or not filename:
            continue

        if is_user_blacklisted(state, username):
            continue

        if is_transfer_blacklisted(state, username, filename):
            log(
                f"  Skipping previously failed remote file: "
                f"{username} / {filename}"
            )
            continue

        log("")
        log(f"Track: {title}")
        log(f"  User: {username}")
        log(f"  File: {filename}")

        known_files = snapshot_download_files()

        try:
            client.enqueue_download(
                username=username,
                filename=filename,
                size=size,
            )
        except Exception as exc:
            log(f"  Queue failed: {exc}")
            record_transfer_failure(
                state,
                username,
                filename,
                exc,
            )
            save_state(state)
            continue

        acquisition["status"] = "downloading"
        save_state(state)

        wanted_key = transfer_key(username, filename)
        deadline = time.monotonic() + TIMEOUT_SECONDS
        last_nonzero_speed = time.monotonic()

        while time.monotonic() < deadline:
            try:
                data = client.get_downloads()
            except Exception as exc:
                log(f"  Unable to read download status: {exc}")
                time.sleep(POLL_SECONDS)
                continue

            matching_transfer = None

            for transfer in extract_transfers(data):
                key = transfer_key(
                    transfer_username(transfer),
                    transfer_filename(transfer),
                )
                if key == wanted_key:
                    matching_transfer = transfer
                    break

            if matching_transfer is None:
                time.sleep(POLL_SECONDS)
                continue

            current_state = transfer_state(matching_transfer).lower()
            total, downloaded, speed = transfer_progress(matching_transfer)

            if speed > 0:
                last_nonzero_speed = time.monotonic()

            if total:
                percent = downloaded / total * 100
                log(f"  State: {current_state}")
                log(
                    f"  Progress: {format_bytes(downloaded)} / "
                    f"{format_bytes(total)} ({percent:.1f}%)"
                )
                log(f"  Speed: {format_speed(speed)}")

            if "succeeded" in current_state:
                local_filename = transfer_local_filename(matching_transfer)
                path = None

                if local_filename:
                    local_path = Path(local_filename)
                    if (
                        local_path.is_file()
                        and (
                            size is None
                            or local_path.stat().st_size == int(size)
                        )
                    ):
                        path = local_path

                if path is None:
                    path = find_downloaded_file(
                        DOWNLOAD_ROOT,
                        filename,
                        expected_size=size,
                        known_files=known_files,
                    )

                if path is None:
                    log(
                        "  Transfer succeeded, but the downloaded "
                        "file was not found yet."
                    )
                    time.sleep(POLL_SECONDS)
                    continue

                actual_size = path.stat().st_size
                acquisition["status"] = "downloaded"
                acquisition["file"] = {
                    "path": str(path),
                    "filename": path.name,
                    "size": actual_size,
                }

                record_user_speed(state, username, speed)
                save_state(state)
                log(f"  Downloaded: {path}")
                return True

            if any(
                failure in current_state
                for failure in FAILURE_STATES
            ):
                log(f"  Download failed: {current_state}")
                record_transfer_failure(
                    state,
                    username,
                    filename,
                    current_state,
                )
                acquisition["status"] = "download_failed"
                acquisition["download_error"] = current_state
                save_state(state)
                break

            if (
                time.monotonic() - last_nonzero_speed
                >= ZERO_SPEED_SECONDS
            ):
                log(
                    f"  Transfer has had zero speed for "
                    f"{ZERO_SPEED_SECONDS} seconds."
                )
                cancel_transfer(client, matching_transfer)
                record_transfer_failure(
                    state,
                    username,
                    filename,
                    "zero_speed",
                )
                acquisition["status"] = "download_failed"
                acquisition["download_error"] = "zero_speed"
                save_state(state)
                break

            time.sleep(POLL_SECONDS)
        else:
            log("  Download timed out.")
            record_transfer_failure(
                state,
                username,
                filename,
                "timeout",
            )
            acquisition["status"] = "download_timeout"
            acquisition["download_error"] = "timeout"
            save_state(state)

    acquisition["status"] = "download_failed"
    acquisition["download_error"] = (
        "All available matched candidates failed or were suppressed."
    )
    save_state(state)
    return False

def main():
    log(
        "Starting download stage."
    )

    state = load_state()

    log(
        "Connecting to slskd..."
    )

    client = SoulseekClient(
        base_url=os.environ.get(
            "SLSKD_URL",
            "http://127.0.0.1:5030",
        ),
        api_key=os.environ.get(
            "SLSKD_API_KEY"
        ),
    )

    processing_tracks = state.get("tracks", [])

    if RELEASE_FILTER:
        processing_tracks = [track for track in processing_tracks if metadata_release_key(track) == RELEASE_FILTER]

    processing_state = {"tracks": processing_tracks}

    # Download files live on the ephemeral Actions runner. Repair any
    # checkpointed download states whose local files disappeared between runs.
    repair_stale_download_states(state, processing_tracks)

    groups = album_groups_from_state(
        processing_state
    )

    album_groups = {
        key: tracks
        for key, tracks in groups.items()
        if len(tracks) >= 2
        and any(
            track.get(
                "acquisition",
                {},
            ).get(
                "status"
            )
            == "matched"
            for track in tracks
        )
    }

    album_track_ids = {
        str(
            track.get(
                "metadata",
                {},
            ).get(
                "id"
            )
        )
        for tracks in album_groups.values()
        for track in tracks
    }

    log(
        f"Found {len(album_groups)} album group(s) "
        "with matched tracks."
    )

    album_successes = 0
    album_failures = 0

    for index, (
        key,
        tracks,
    ) in enumerate(
        album_groups.items(),
        start=1,
    ):
        log("")
        log(
            f"=== Album "
            f"{index}/{len(album_groups)} ==="
        )

        if process_album(
            client,
            state,
            tracks,
        ):
            album_successes += 1
        else:
            album_failures += 1

        save_state(state)

    pending_individual = []

    for track in processing_tracks:
        track_id = str(
            track.get(
                "metadata",
                {},
            ).get(
                "id"
            )
        )

        acquisition = track.get(
            "acquisition",
            {},
        )

        status = acquisition.get(
            "status"
        )

        if (
            status == "matched"
            and track_id
            not in album_track_ids
        ):
            pending_individual.append(
                track
            )

    individual_successes = 0
    individual_failures = 0

    if pending_individual:
        log("")
        log(
            f"Processing "
            f"{len(pending_individual)} "
            "individual track(s)."
        )

    for track in pending_individual:
        if process_individual_track(
            client,
            state,
            track,
        ):
            individual_successes += 1
        else:
            individual_failures += 1

        save_state(state)

    log("")
    log(
        "Download stage finished."
    )
    log(
        f"Albums succeeded: "
        f"{album_successes}"
    )
    log(
        f"Albums failed: "
        f"{album_failures}"
    )
    log(
        f"Individual tracks succeeded: "
        f"{individual_successes}"
    )
    log(
        f"Individual tracks failed: "
        f"{individual_failures}"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())