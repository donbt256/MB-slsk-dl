import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from release import release_key
from config import integer, string


RELEASE_FILTER = os.environ.get("RELEASE_KEY")
from urllib.parse import quote

import requests
import shutil
import subprocess


STATE_FILE = Path(string("paths.state_file", default="state/tracks.json"))

GITHUB_API = "https://api.github.com"

REPO_TARGET_BYTES = integer("library.soft_rollover_bytes", "GITHUB_LIBRARY_SOFT_ROLLOVER_BYTES", 900 * 1024 * 1024)
ALBUM_ATOMIC_LIMIT_BYTES = integer("library.hard_rollover_bytes", "GITHUB_LIBRARY_HARD_ROLLOVER_BYTES", 1 * 1024 * 1024 * 1024)
GITHUB_MAX_FILE_BYTES = integer("library.max_file_bytes", "GITHUB_MAX_FILE_BYTES", 100 * 1024 * 1024)
ALBUM_ART_RESERVE_BYTES = integer("library.artwork_reserve_bytes", "GITHUB_LIBRARY_ARTWORK_RESERVE_BYTES", 2 * 1024 * 1024)

LIBRARY_PREFIX = string("library.repo_prefix", "GITHUB_LIBRARY_PREFIX", "music-library-")
LIBRARY_START_NUMBER = integer("library.start_number", "GITHUB_LIBRARY_START", 1)
LIBRARY_NUMBER_WIDTH = integer("library.number_width", "GITHUB_LIBRARY_NUMBER_WIDTH", 3)
REQUEST_TIMEOUT = integer("github.request_timeout_seconds", "GITHUB_REQUEST_TIMEOUT_SECONDS", 60)


def log(message=""):
    print(message, flush=True)


def load_state():
    if not STATE_FILE.exists():
        raise SystemExit(
            f"Missing {STATE_FILE}"
        )

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


def format_bytes(value):
    value = float(value or 0)

    units = [
        "B",
        "KiB",
        "MiB",
        "GiB",
    ]

    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"

        value /= 1024

    return f"{value:.1f} GiB"


def normalize_path(value):
    value = str(value or "")

    value = value.replace(
        "\\",
        "/",
    )

    value = re.sub(
        r"/+",
        "/",
        value,
    )

    value = value.strip("/")

    return value


def sanitize_component(value):
    value = str(value or "").strip()

    value = value.replace(
        "/",
        "_",
    )

    value = value.replace(
        "\\",
        "_",
    )

    value = value.replace(
        "\x00",
        "",
    )

    if value in {"", ".", ".."}:
        return "_"

    return value


def metadata(track):
    return track.get(
        "metadata",
        track,
    )


def track_id(track):
    data = metadata(track)

    return data.get("id") or (
        f"{data.get('artist', '')}\x1f"
        f"{data.get('album', '')}\x1f"
        f"{data.get('title', '')}"
    )


def album_key(track):
    data = metadata(track)
    sources = track.get("sources", {})
    release_ids = sources.get("release_ids", [])

    if release_ids:
        return ("metadata_album", str(release_ids[0]))

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

    # Legacy state fallback.
    return ("metadata_album", artist.casefold(), album.casefold())


def is_downloaded(track):
    acquisition = track.get(
        "acquisition",
        {},
    )

    status = acquisition.get(
        "status"
    )

    return status in {
        "downloaded",
        "ready_to_publish",
    }


def already_published(track):
    acquisition = track.get(
        "acquisition",
        {}
    )

    return acquisition.get(
        "status"
    ) == "published"


def get_local_file(track):
    acquisition = track.get(
        "acquisition",
        {},
    )

    file_info = acquisition.get(
        "file"
    )

    if not isinstance(
        file_info,
        dict,
    ):
        return None

    path = (
        file_info.get("path")
        or file_info.get("local_path")
        or file_info.get("localPath")
    )

    if not path:
        return None

    path = Path(path)

    if not path.is_file():
        return None

    return path


def get_download_size(track):
    path = get_local_file(track)

    if path is None:
        return 0

    return path.stat().st_size


def album_groups(tracks):
    """
    Group every non-published track by album.

    Including not-yet-downloaded tracks is important: it lets the
    publisher detect an incomplete album instead of publishing only
    the subset that happened to download successfully.
    """
    groups = {}

    for index, track in enumerate(tracks):
        if already_published(track):
            continue

        key = album_key(track)

        if key is None:
            continue

        groups.setdefault(
            key,
            {
                "first_index": index,
                "tracks": [],
            },
        )

        groups[key]["tracks"].append(track)

    ordered = list(groups.values())

    ordered.sort(
        key=lambda item: item["first_index"]
    )

    return ordered


def build_items(tracks):
    """
    Build the publishing queue in original track order.

    Multi-track albums are atomic: every non-published track in the
    album must be downloaded before any track from that album enters
    the publishing queue.

    A one-track release remains an individual item.
    """
    groups = album_groups(tracks)

    track_to_group = {}

    for group in groups:
        for track in group["tracks"]:
            track_to_group[id(track)] = group

    items = []
    consumed = set()

    for index, track in enumerate(tracks):
        if id(track) in consumed:
            continue

        if already_published(track):
            continue

        group = track_to_group.get(id(track))

        if group is not None and len(group["tracks"]) >= 2:
            group_tracks = group["tracks"]

            if not all(
                is_downloaded(member)
                and get_local_file(member) is not None
                for member in group_tracks
            ):
                downloaded_count = sum(
                    is_downloaded(member)
                    and get_local_file(member) is not None
                    for member in group_tracks
                )

                log(
                    f"Skipping incomplete album: "
                    f"{item_artist_name({'tracks': group_tracks})} - "
                    f"{item_album_name({'tracks': group_tracks})} "
                    f"({downloaded_count}/{len(group_tracks)} tracks downloaded)"
                )

                for member in group_tracks:
                    consumed.add(id(member))

                continue

            for member in group_tracks:
                consumed.add(id(member))

            items.append(
                {
                    "type": "album",
                    "first_index": group["first_index"],
                    "tracks": group_tracks,
                }
            )

        elif is_downloaded(track) and get_local_file(track) is not None:
            consumed.add(id(track))

            items.append(
                {
                    "type": "track",
                    "first_index": index,
                    "tracks": [track],
                }
            )

    items.sort(
        key=lambda item: item["first_index"]
    )

    return items

def item_size(item):
    size = sum(
        get_download_size(track)
        for track in item["tracks"]
    )

    first = metadata(item["tracks"][0])
    album_art = first.get("album_art")
    if isinstance(album_art, dict) and album_art.get("url"):
        size += ALBUM_ART_RESERVE_BYTES

    return size


def item_album_name(item):
    track = item["tracks"][0]
    data = metadata(track)

    return str(
        data.get("album")
        or "Unknown Album"
    )


def item_artist_name(item):
    track = item["tracks"][0]
    data = metadata(track)

    return str(
        data.get("album_artist")
        or data.get("artist")
        or "Unknown Artist"
    )


def is_atomic_album(item):
    if item["type"] != "album":
        return False

    return (
        item_size(item)
        <= ALBUM_ATOMIC_LIMIT_BYTES
    )


def is_transient_upload_error(message):
    """Return True for GitHub upload failures that are safe to retry."""
    retry_statuses = {
        408,
        429,
        500,
        502,
        503,
        504,
    }

    if any(
        f"failed with {status}:" in message
        for status in retry_statuses
    ):
        return True

    # GitHub can return this as a 403 when its repository/ruleset
    # validation service times out. This is transient, not an auth
    # or permission failure, and GitHub explicitly asks the caller
    # to try the request again.
    return (
        "failed with 403:" in message
        and "Timed out validating rule" in message
    )


class GitHubClient:
    def __init__(self, token):
        if not token:
            raise SystemExit(
                "GIT_PAT is required."
            )

        self.session = requests.Session()

        self.session.headers.update(
            {
                "Authorization": (
                    f"Bearer {token}"
                ),
                "Accept": (
                    "application/vnd.github+json"
                ),
                "X-GitHub-Api-Version": (
                    "2022-11-28"
                ),
                "User-Agent": (
                    "spot-slsk-publisher"
                ),
            }
        )

        self.owner = self._get_owner()

    def request(
        self,
        method,
        path,
        **kwargs,
    ):
        url = (
            f"{GITHUB_API}"
            f"{path}"
        )

        response = self.session.request(
            method,
            url,
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"GitHub API {method} {path} "
                f"failed with "
                f"{response.status_code}: "
                f"{detail}"
            )

        if not response.content:
            return None

        return response.json()

    def _get_owner(self):
        configured = string("library.owner", "GITHUB_LIBRARY_OWNER", "")

        if configured:
            return configured

        source_repo = os.environ.get(
            "GITHUB_REPO"
        )

        if source_repo and "/" in source_repo:
            return source_repo.split(
                "/",
                1,
            )[0]

        data = self.request(
            "GET",
            "/user",
        )

        login = data.get(
            "login"
        )

        if not login:
            raise RuntimeError(
                "Could not determine GitHub "
                "account owner."
            )

        return login

    def repo_exists(self, repo):
        path = (
            f"/repos/"
            f"{quote(self.owner, safe='')}/"
            f"{quote(repo, safe='')}"
        )

        response = self.session.get(
            f"{GITHUB_API}{path}",
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 404:
            return False

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"Unable to check repository "
                f"{repo}: "
                f"{response.status_code}: "
                f"{detail}"
            )

        return True

    def create_repo(self, repo):
        log(
            f"Creating library repository "
            f"{self.owner}/{repo}"
        )

        data = self.request(
            "POST",
            "/user/repos",
            json={
                "name": repo,
                "description": (
                    "Music library generated "
                    "by spot-slsk"
                ),
                "private": True,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
                "has_discussions": False,
                "auto_init": True,
            },
        )

        return data

    def ensure_repo(self, repo):
        if self.repo_exists(repo):
            return

        self.create_repo(repo)

        # GitHub may need a short moment before
        # the repository is immediately accessible.
        for _ in range(10):
            if self.repo_exists(repo):
                return

            time.sleep(1)

        raise RuntimeError(
            f"Repository {repo} was created "
            f"but did not become available."
        )

    def list_tree(self, repo):
        repo_path = (
            f"/repos/"
            f"{quote(self.owner, safe='')}/"
            f"{quote(repo, safe='')}"
        )

        repository = self.request(
            "GET",
            repo_path,
        )

        default_branch = (
            repository.get(
                "default_branch"
            )
            or "main"
        )

        branch_path = (
            f"{repo_path}/git/refs/heads/"
            f"{quote(default_branch, safe='')}"
        )

        ref = self.request(
            "GET",
            branch_path,
        )

        commit_sha = (
            ref.get("object", {})
            .get("sha")
        )

        if not commit_sha:
            return {
                "branch": default_branch,
                "files": [],
                "bytes": 0,
            }

        commit = self.request(
            "GET",
            f"{repo_path}/git/commits/"
            f"{commit_sha}",
        )

        tree_sha = (
            commit.get("tree", {})
            .get("sha")
        )

        if not tree_sha:
            return {
                "branch": default_branch,
                "files": [],
                "bytes": 0,
            }

        tree = self.request(
            "GET",
            f"{repo_path}/git/trees/"
            f"{tree_sha}",
            params={
                "recursive": "1"
            },
        )

        files = []

        total_bytes = 0

        for entry in tree.get(
            "tree",
            [],
        ):
            if entry.get("type") != "blob":
                continue

            size = int(
                entry.get("size")
                or 0
            )

            files.append(
                {
                    "path": entry.get(
                        "path"
                    ),
                    "size": size,
                    "sha": entry.get("sha"),
                }
            )

            total_bytes += size

        return {
            "branch": default_branch,
            "files": files,
            "bytes": total_bytes,
        }

    def get_repo_state(self, repo):
        self.ensure_repo(repo)

        tree = self.list_tree(
            repo
        )

        return {
            "name": repo,
            "branch": tree[
                "branch"
            ],
            "bytes": tree[
                "bytes"
            ],
            "files": tree[
                "files"
            ],
        }

    def get_branch_head(self, repo, branch):
        repo_path = (
            f"/repos/"
            f"{quote(self.owner, safe='')}/"
            f"{quote(repo, safe='')}"
        )
        ref = self.request(
            "GET",
            f"{repo_path}/git/ref/heads/{quote(branch, safe='')}",
        )
        sha = ref.get("object", {}).get("sha")
        if not sha:
            raise RuntimeError(
                f"Could not determine {repo}/{branch} HEAD."
            )
        return sha

    def _write_request(self, method, path, **kwargs):
        max_attempts = int(
            integer("github.upload_retries", "GITHUB_UPLOAD_RETRIES", 5)
        )

        for attempt in range(1, max_attempts + 1):
            try:
                return self.request(
                    method,
                    path,
                    **kwargs,
                )
            except RuntimeError as exc:
                message = str(exc)
                if (
                    not is_transient_upload_error(message)
                    or attempt >= max_attempts
                ):
                    raise

                delay = 2 ** attempt
                log(
                    f"  GitHub Git-data write failed transiently "
                    f"(attempt {attempt}/{max_attempts}): {message}"
                )
                log(f"  Retrying Git-data write in {delay} seconds...")
                time.sleep(delay)

            except requests.RequestException as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"GitHub Git-data write failed after "
                        f"{max_attempts} attempts: {exc}"
                    ) from exc

                delay = 2 ** attempt
                log(
                    f"  GitHub Git-data connection error "
                    f"(attempt {attempt}/{max_attempts}): {exc}"
                )
                log(f"  Retrying Git-data write in {delay} seconds...")
                time.sleep(delay)

        raise RuntimeError("GitHub Git-data write failed.")

    def _git_env(self):
        token = os.environ.get("GIT_PAT")
        if not token:
            raise RuntimeError("GIT_PAT is required for Git push.")

        encoded = base64.b64encode(
            f"x-access-token:{token}".encode("utf-8")
        ).decode("ascii")
        env = os.environ.copy()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
        return env

    def _run_git(self, args, cwd=None):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=self._git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode(
                "utf-8", errors="replace"
            ).strip()
            raise RuntimeError(
                f"git {' '.join(args)} failed with exit code "
                f"{result.returncode}: {detail}"
            )
        return result.stdout

    def _local_repo(self, repo):
        if not hasattr(self, "_local_repos"):
            self._local_repos = {}

        existing = self._local_repos.get(repo)
        if existing:
            return existing

        cache_root = Path(
            os.environ.get(
                "GITHUB_LOCAL_REPO_CACHE",
                "/tmp/mb-slsk-library-repos",
            )
        )
        cache_root.mkdir(parents=True, exist_ok=True)
        path = cache_root / repo
        remote = f"https://github.com/{self.owner}/{repo}.git"

        if not (path / ".git").is_dir():
            if path.exists():
                shutil.rmtree(path)
            self._run_git(
                [
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    remote,
                    str(path),
                ]
            )
        else:
            self._run_git(["fetch", "origin"], cwd=path)

        self._local_repos[repo] = path
        return path

    def _staging_dir(self, repo):
        root = Path(
            os.environ.get(
                "GITHUB_LOCAL_REPO_CACHE",
                "/tmp/mb-slsk-library-repos",
            )
        )
        staging = root.parent / "mb-slsk-library-staging" / repo
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def create_blob(self, repo, content):
        # Keep the existing publish_item interface, but create the Git blob
        # locally. GitHub's REST /git/blobs endpoint rejects sufficiently
        # large request bodies even when the individual file is below GitHub's
        # 100 MiB Git blob limit. Normal Git transport handles these files.
        path = self._local_repo(repo)
        staging = self._staging_dir(repo)

        temporary = staging / f"payload-{time.time_ns()}"
        temporary.write_bytes(content)
        try:
            sha = self._run_git(
                ["hash-object", "-w", str(temporary)],
                cwd=path,
            ).decode("ascii").strip()
        finally:
            temporary.unlink(missing_ok=True)

        retained = staging / sha
        if not retained.exists():
            retained.write_bytes(content)
        return sha


    def create_tree_commit(self, repo, branch, entries, message):
        path = self._local_repo(repo)

        # Refresh the remote tracking branch and reconstruct the index from
        # it without checking out the existing audio blobs.
        self._run_git(["fetch", "origin", branch], cwd=path)
        self._run_git(["read-tree", f"origin/{branch}"], cwd=path)

        staging = self._staging_dir(repo)
        additions = []
        for entry in entries:
            relative = Path(entry["path"])

            # A null blob SHA is the existing publisher's deletion marker.
            # Use the local Git index so cleanup operations remain atomic.
            if entry.get("sha") is None:
                self._run_git(
                    ["rm", "--cached", "--ignore-unmatch", "--", entry["path"]],
                    cwd=path,
                )
                continue

            source = staging / entry["sha"]
            if not source.is_file():
                raise RuntimeError(
                    f"Missing staged Git object for {entry['path']}"
                )

            # The blob already exists in the local Git object database.
            # Put that exact object into the index without copying/re-reading
            # a potentially 100 MiB audio file through the working tree.
            self._run_git(
                [
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "100644",
                    entry["sha"],
                    entry["path"],
                ],
                cwd=path,
            )
            additions.append(entry["path"])

        # Check only the index. The local clone intentionally may contain
        # stale/untracked working-tree files from previous atomic commits.
        staged_changes = self._run_git(
            ["diff", "--cached", "--name-only"],
            cwd=path,
        ).decode("utf-8", errors="replace").strip()
        if not staged_changes:
            return self._run_git(
                ["rev-parse", f"origin/{branch}"],
                cwd=path,
            ).decode("ascii").strip()
            return self._run_git(
                ["rev-parse", f"origin/{branch}"],
                cwd=path,
            ).decode("ascii").strip()

        self._run_git(
            [
                "-c", "user.name=MB-slsk-dl",
                "-c", "user.email=actions@users.noreply.github.com",
                "commit", "-m", message,
            ],
            cwd=path,
        )

        try:
            self._run_git(
                ["push", "origin", f"HEAD:{branch}"],
                cwd=path,
            )
        except RuntimeError as exc:
            message_text = str(exc)
            if "non-fast-forward" not in message_text and "rejected" not in message_text:
                raise

            self._run_git(["fetch", "origin", branch], cwd=path)
            self._run_git(["rebase", f"origin/{branch}"], cwd=path)
            self._run_git(
                ["push", "origin", f"HEAD:{branch}"],
                cwd=path,
            )

        # The staged copies are no longer needed after the push.
        for entry in entries:
            (staging / entry["sha"]).unlink(missing_ok=True)

        return self._run_git(
            ["rev-parse", "HEAD"],
            cwd=path,
        ).decode("ascii").strip()

    def put_file(
        self,
        repo,
        path,
        local_file,
        branch,
        message,
    ):
        local_file = Path(
            local_file
        )

        encoded = base64.b64encode(
            local_file.read_bytes()
        ).decode("ascii")

        api_path = (
            f"/repos/"
            f"{quote(self.owner, safe='')}/"
            f"{quote(repo, safe='')}/"
            f"contents/"
            f"{quote(normalize_path(path), safe='/')}"
        )

        existing_sha = None

        response = self.session.get(
            f"{GITHUB_API}{api_path}",
            params={
                "ref": branch
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 200:
            try:
                existing_sha = response.json().get(
                    "sha"
                )
            except Exception:
                existing_sha = None

        elif response.status_code != 404:
            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"Unable to inspect "
                f"{repo}/{path}: "
                f"{response.status_code}: "
                f"{detail}"
            )

        payload = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }

        if existing_sha:
            payload["sha"] = existing_sha

        max_attempts = int(
            os.environ.get(
                "GITHUB_UPLOAD_RETRIES",
                "5",
            )
        )

        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                self.request(
                    "PUT",
                    api_path,
                    json=payload,
                )
                return

            except RuntimeError as exc:
                last_error = exc
                message = str(exc)

                transient = is_transient_upload_error(message)

                # GitHub can return this 403 while a repository ruleset
                # validation request times out. It is transient; ordinary
                # permission/authentication 403s must still fail immediately.
                if (
                    "failed with 403:" in message
                    and "Timed out validating rule" in message
                ):
                    transient = True

                if not transient or attempt >= max_attempts:
                    raise

                delay = 2 ** attempt

                log(
                    f"  GitHub upload failed transiently "
                    f"(attempt {attempt}/{max_attempts}): "
                    f"{message}"
                )
                log(
                    f"  Retrying upload in {delay} seconds..."
                )
                time.sleep(delay)

            except requests.RequestException as exc:
                last_error = exc

                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"GitHub upload failed after "
                        f"{max_attempts} attempts: {exc}"
                    ) from exc

                delay = 2 ** attempt

                log(
                    f"  GitHub upload connection error "
                    f"(attempt {attempt}/{max_attempts}): "
                    f"{exc}"
                )
                log(
                    f"  Retrying upload in {delay} seconds..."
                )
                time.sleep(delay)

        if last_error is not None:
            raise last_error


    def put_bytes(
        self,
        repo,
        path,
        content,
        branch,
        message,
    ):
        api_path = (
            f"/repos/"
            f"{quote(self.owner, safe='')}/"
            f"{quote(repo, safe='')}/"
            f"contents/"
            f"{quote(normalize_path(path), safe='/')}"
        )

        response = self.session.get(
            f"{GITHUB_API}{api_path}",
            params={"ref": branch},
            timeout=REQUEST_TIMEOUT,
        )

        existing_sha = None
        if response.status_code == 200:
            existing_sha = response.json().get("sha")
        elif response.status_code != 404:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(
                f"Unable to inspect {repo}/{path}: "
                f"{response.status_code}: {detail}"
            )

        payload = {
            "message": message,
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        max_attempts = int(os.environ.get("GITHUB_UPLOAD_RETRIES", "5"))

        for attempt in range(1, max_attempts + 1):
            try:
                self.request("PUT", api_path, json=payload)
                return
            except RuntimeError as exc:
                message_text = str(exc)
                transient = is_transient_upload_error(message_text)

                if (
                    "failed with 403:" in message_text
                    and "Timed out validating rule" in message_text
                ):
                    transient = True

                if not transient or attempt >= max_attempts:
                    raise
                delay = 2 ** attempt
                log(
                    f"  GitHub upload failed transiently "
                    f"(attempt {attempt}/{max_attempts}): {message_text}"
                )
                log(f"  Retrying upload in {delay} seconds...")
                time.sleep(delay)
            except requests.RequestException as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"GitHub upload failed after "
                        f"{max_attempts} attempts: {exc}"
                    ) from exc
                delay = 2 ** attempt
                log(
                    f"  GitHub upload connection error "
                    f"(attempt {attempt}/{max_attempts}): {exc}"
                )
                log(f"  Retrying upload in {delay} seconds...")
                time.sleep(delay)


def library_repo_number(repo):
    suffix = repo[
        len(LIBRARY_PREFIX):
    ]

    try:
        return int(suffix)
    except ValueError:
        return None


def discover_library_repos(client):
    repos = []

    number = LIBRARY_START_NUMBER

    while True:
        repo = (
            f"{LIBRARY_PREFIX}"
            f"{number:03d}"
        )

        if not client.repo_exists(repo):
            break

        repos.append(repo)
        number += 1

    return repos


def ensure_initial_repo(
    client,
    repos,
):
    if repos:
        return repos

    repo = (
        f"{LIBRARY_PREFIX}"
        f"{LIBRARY_START_NUMBER:03d}"
    )

    client.ensure_repo(
        repo
    )

    repos.append(repo)

    return repos


def refresh_repo_state(
    client,
    repo_states,
    repo,
):
    state = client.get_repo_state(
        repo
    )

    repo_states[repo] = state

    return state


def available_bytes(
    repo_state,
):
    return max(
        0,
        REPO_TARGET_BYTES
        - repo_state["bytes"],
    )


def next_repo_name(
    existing_repos,
):
    highest = (
        LIBRARY_START_NUMBER - 1
    )

    for repo in existing_repos:
        number = library_repo_number(
            repo
        )

        if number is not None:
            highest = max(
                highest,
                number,
            )

    return (
        f"{LIBRARY_PREFIX}"
        f"{highest + 1:03d}"
    )


def ensure_next_repo(
    client,
    repos,
    repo_states,
):
    repo = next_repo_name(
        repos
    )

    client.ensure_repo(
        repo
    )

    state = client.get_repo_state(
        repo
    )

    repos.append(repo)
    repo_states[repo] = state

    return repo


def is_audio_library_path(path):
    return Path(path).suffix.lower() in {
        ".mp3", ".flac", ".m4a", ".aac", ".ogg",
        ".opus", ".wav", ".alac", ".aiff", ".ape", ".wma",
    }


def cleanup_partial_albums(client, state, repos, repo_states):
    """
    Remove incomplete multi-track albums left by older runs.

    The deletion is committed and the branch ref is advanced in the same
    operation. Artwork and any other files under a partial album directory
    are removed along with the audio files.
    """
    expected = {}

    for track in state.get("tracks", []):
        data = metadata(track)
        artist = str(
            data.get("album_artist")
            or data.get("artist")
            or ""
        ).strip()
        album = str(data.get("album") or "").strip()

        if not artist or not album:
            continue

        key = (artist.casefold(), album.casefold())
        expected.setdefault(
            key,
            {"artist": artist, "album": album, "tracks": []},
        )["tracks"].append(track)

    partial_count = 0

    for album in expected.values():
        expected_count = len(album["tracks"])
        if expected_count < 2:
            continue

        prefix = normalize_path(
            f"{sanitize_component(album['artist'])}/"
            f"{sanitize_component(album['album'])}/"
        )

        for repo in list(repos):
            repo_state = repo_states[repo]
            existing_audio = [
                entry for entry in repo_state.get("files", [])
                if is_audio_library_path(entry.get("path", ""))
                and normalize_path(entry.get("path", "")).startswith(prefix)
            ]

            if not existing_audio or len(existing_audio) >= expected_count:
                continue

            existing_all = [
                entry for entry in repo_state.get("files", [])
                if normalize_path(entry.get("path", "")).startswith(prefix)
            ]

            log(
                f"Cleaning partial album: "
                f"{album['artist']} - {album['album']} "
                f"({len(existing_audio)}/{expected_count} tracks) "
                f"from {repo}"
            )

            entries = [
                {
                    "path": entry["path"],
                    "mode": "100644",
                    "type": "blob",
                    "sha": None,
                }
                for entry in existing_all
            ]

            client.create_tree_commit(
                repo=repo,
                branch=repo_state["branch"],
                entries=entries,
                message=(
                    f"Remove partial album: "
                    f"{album['artist']} - {album['album']}"
                ),
            )

            removed_paths = {
                normalize_path(entry["path"])
                for entry in existing_all
            }
            repo_state["files"] = [
                entry for entry in repo_state.get("files", [])
                if normalize_path(entry.get("path", ""))
                not in removed_paths
            ]
            repo_state["bytes"] = sum(
                int(entry.get("size") or 0)
                for entry in repo_state["files"]
            )

            for track in album["tracks"]:
                acquisition = track.setdefault("acquisition", {})
                library = acquisition.get("library") or {}
                if (
                    acquisition.get("status") == "published"
                    and library.get("repo") == repo
                    and normalize_path(library.get("path", "")) in removed_paths
                ):
                    acquisition["status"] = "pending"
                    acquisition.pop("library", None)

            partial_count += 1

    if partial_count:
        save_state(state)
        log(f"Cleaned {partial_count} partial album(s).")
def cleanup_duplicate_release(
    client,
    item,
    destination_repo,
    repos,
    repo_states,
):
    """
    After a release is safely committed to its destination repository,
    remove any older copy of the same release from every other library
    repository.

    Albums are removed by their exact canonical directory. Individual
    releases are removed by their exact canonical track path.
    """
    first = metadata(item["tracks"][0])
    artist = sanitize_component(
        first.get("album_artist")
        or first.get("artist")
        or "Unknown Artist"
    )
    album = sanitize_component(
        first.get("album")
        or "Unknown Album"
    )
    album_prefix = normalize_path(f"{artist}/{album}/")

    target_paths = set()
    if item["type"] == "album":
        target_paths = {
            normalize_path(entry.get("path", ""))
            for entry in repo_states[destination_repo].get("files", [])
            if normalize_path(entry.get("path", "")).startswith(album_prefix)
        }
    else:
        target_paths = {
            relative_library_path(item["tracks"][0])
        }

    if not target_paths:
        return

    for repo in list(repos):
        if repo == destination_repo:
            continue

        repo_state = repo_states[repo]
        if item["type"] == "album":
            stale = [
                entry for entry in repo_state.get("files", [])
                if normalize_path(entry.get("path", "")).startswith(album_prefix)
            ]
        else:
            stale = [
                entry for entry in repo_state.get("files", [])
                if normalize_path(entry.get("path", "")) in target_paths
            ]

        if not stale:
            continue

        entries = [
            {
                "path": entry["path"],
                "mode": "100644",
                "type": "blob",
                "sha": None,
            }
            for entry in stale
        ]

        log(
            f"Removing stale {item['type']} copy of "
            f"{item_artist_name(item)} - {item_album_name(item)} "
            f"from {repo}"
        )

        client.create_tree_commit(
            repo=repo,
            branch=repo_state["branch"],
            entries=entries,
            message=(
                f"Remove duplicate: "
                f"{item_artist_name(item)} - "
                f"{item_album_name(item)}"
            ),
        )

        removed = {
            normalize_path(entry["path"])
            for entry in stale
        }
        repo_state["files"] = [
            entry for entry in repo_state.get("files", [])
            if normalize_path(entry.get("path", "")) not in removed
        ]
        repo_state["bytes"] = sum(
            int(entry.get("size") or 0)
            for entry in repo_state["files"]
        )

def track_identity_fingerprint(track):
    data = metadata(track)
    return (
        str(
            data.get("artist")
            or ""
        ).strip().casefold(),
        tuple(
            str(value).strip().casefold()
            for value in data.get("artists", [])
            if value is not None
        ),
        str(
            data.get("album_artist")
            or ""
        ).strip().casefold(),
        str(
            data.get("album")
            or ""
        ).strip().casefold(),
        str(
            data.get("title")
            or ""
        ).strip().casefold(),
        data.get("track_number"),
        data.get("disc_number"),
        data.get("duration_ms"),
        str(
            data.get("isrc")
            or ""
        ).strip().casefold(),
    )


def mark_existing_library_duplicates(tracks, repo_states):
    """
    Reuse an existing library file only when the stored metadata metadata
    identifies the same recording. A path collision with different metadata
    is an error rather than an implicit overwrite/deduplication.
    """
    path_to_existing = {}

    for track in tracks:
        acquisition = track.get("acquisition", {})
        if acquisition.get("status") != "published":
            continue

        library = acquisition.get("library") or {}
        path = normalize_path(library.get("path", ""))
        if path:
            path_to_existing.setdefault(path, []).append(track)

    # Include library files that are not represented in current state so
    # accidental path collisions cannot silently overwrite them.
    for repo, repo_state in repo_states.items():
        for entry in repo_state.get("files", []):
            path = normalize_path(entry.get("path", ""))
            if path:
                path_to_existing.setdefault(path, [])

    changed = 0

    for track in tracks:
        acquisition = track.setdefault("acquisition", {})
        if acquisition.get("status") == "published":
            continue

        try:
            path = relative_library_path(track)
        except RuntimeError:
            continue

        existing_tracks = path_to_existing.get(path)
        if existing_tracks is None:
            continue

        fingerprint = track_identity_fingerprint(track)
        matching = [
            existing
            for existing in existing_tracks
            if track_identity_fingerprint(existing) == fingerprint
        ]

        if matching:
            existing = matching[0]
            existing_library = existing.get("acquisition", {}).get("library", {})
            acquisition["status"] = "published"
            acquisition["library"] = {
                "repo": existing_library.get("repo"),
                "path": path,
                "deduplicated": True,
            }
            changed += 1
            continue

        # The canonical library path is the library identity:
        # Artist/Album/track number + title. metadata can expose different
        # duration/ISRC metadata for the same canonical recording across
        # releases, so those fields must not make a valid replacement fail.
        #
        # Leave the track pending here. publish_item() will atomically write
        # the new blob at this exact path, replacing the existing file in the
        # tree. If the existing file is represented in state, the normal
        # duplicate logic above already handles exact metadata matches.
        log(
            f"  Existing library path will be replaced: {path}"
        )

    if changed:
        log(
            f"Reused {changed} existing library file(s) "
            "instead of uploading duplicate metadata tracks."
        )

    return changed

def relative_library_path(track):
    data = metadata(track)

    artist = sanitize_component(
        data.get("album_artist")
        or data.get("artist")
        or "Unknown Artist"
    )

    album = sanitize_component(
        data.get("album")
        or "Unknown Album"
    )

    title = sanitize_component(
        data.get("title")
        or "Unknown Track"
    )

    path = get_local_file(
        track
    )

    if path is None:
        raise RuntimeError(
            "Track has no local file."
        )

    extension = path.suffix

    if not extension:
        extension = ""

    track_number = data.get(
        "track_number"
    )

    if track_number is not None:
        try:
            track_number = int(
                track_number
            )

            disc_number = data.get("disc_number")
            try:
                disc_number = int(disc_number)
            except (TypeError, ValueError):
                disc_number = 1

            if disc_number > 1:
                filename = (
                    f"{disc_number:02d}-{track_number:02d} - "
                    f"{title}"
                    f"{extension}"
                )
            else:
                filename = (
                    f"{track_number:02d} - "
                    f"{title}"
                    f"{extension}"
                )

        except (
            TypeError,
            ValueError,
        ):
            filename = (
                f"{title}"
                f"{extension}"
            )
    else:
        filename = (
            f"{title}"
            f"{extension}"
        )

    return normalize_path(
        f"{artist}/"
        f"{album}/"
        f"{filename}"
    )


def mark_published(
    track,
    repo,
    path,
):
    acquisition = track.setdefault(
        "acquisition",
        {},
    )

    acquisition[
        "status"
    ] = "published"

    acquisition[
        "library"
    ] = {
        "repo": repo,
        "path": path,
    }


def set_pending_publish(
    track,
):
    acquisition = track.setdefault(
        "acquisition",
        {},
    )

    acquisition[
        "status"
    ] = "ready_to_publish"


def _normalize_artwork_match(value):
    value = str(value or "").casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _artwork_response(url):
    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "MB-slsk-dl/1.0"},
    )
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").casefold()
    if content_type and not content_type.startswith("image/"):
        raise RuntimeError(
            f"unexpected content type: {content_type}"
        )

    if not response.content:
        raise RuntimeError("empty response")

    return response.content


def _musicbrainz_release_group_art_url(track):
    data = metadata(track)
    sources = track.get("sources", {})

    release_group_id = (
        data.get("release_group_id")
        or data.get("releaseGroupId")
    )

    release_group_ids = sources.get("release_group_ids", [])
    if not release_group_id and release_group_ids:
        release_group_id = release_group_ids[0]

    if not release_group_id:
        return None

    return (
        "https://coverartarchive.org/release-group/"
        f"{quote(str(release_group_id), safe='')}/front-500"
    )


def _itunes_artwork_url(track):
    data = metadata(track)
    artist = str(
        data.get("album_artist")
        or data.get("artist")
        or ""
    ).strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return None

    response = requests.get(
        "https://itunes.apple.com/search",
        params={
            "term": f"{artist} {album}",
            "entity": "album",
            "limit": 10,
            "country": "US",
        },
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "MB-slsk-dl/1.0"},
    )
    response.raise_for_status()

    results = response.json().get("results", [])
    target_artist = _normalize_artwork_match(artist)
    target_album = _normalize_artwork_match(album)

    best_url = None
    best_score = -1

    for result in results:
        result_artist = _normalize_artwork_match(
            result.get("artistName")
        )
        result_album = _normalize_artwork_match(
            result.get("collectionName")
        )

        score = 0
        if result_artist == target_artist:
            score += 2
        if result_album == target_album:
            score += 3

        artwork_url = result.get("artworkUrl100")
        if score > best_score and artwork_url:
            best_score = score
            best_url = artwork_url

    # Require an exact album match and an exact or sufficiently close
    # artist match; this prevents a similarly named album from supplying
    # the wrong cover.
    if best_score < 5:
        return None

    return re.sub(
        r"/(?:100x100|100x100bb)\.",
        "/1200x1200bb.",
        best_url,
    )


def _deezer_artwork_url(track):
    data = metadata(track)
    artist = str(
        data.get("album_artist")
        or data.get("artist")
        or ""
    ).strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return None

    response = requests.get(
        "https://api.deezer.com/search/album",
        params={
            "q": f'artist:"{artist}" album:"{album}"',
            "limit": 10,
        },
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "MB-slsk-dl/1.0"},
    )
    response.raise_for_status()

    results = response.json().get("data", [])
    target_artist = _normalize_artwork_match(artist)
    target_album = _normalize_artwork_match(album)

    best_url = None
    best_score = -1

    for result in results:
        result_artist = _normalize_artwork_match(
            (result.get("artist") or {}).get("name")
        )
        result_album = _normalize_artwork_match(
            result.get("title")
        )

        score = 0
        if result_artist == target_artist:
            score += 2
        if result_album == target_album:
            score += 3

        artwork_url = (
            result.get("cover_xl")
            or result.get("cover_big")
            or result.get("cover_medium")
        )

        if score > best_score and artwork_url:
            best_score = score
            best_url = artwork_url

    if best_score < 5:
        return None

    return best_url


def fetch_album_art(track):
    data = metadata(track)
    album_art = data.get("album_art")
    candidates = []

    if isinstance(album_art, dict) and album_art.get("url"):
        candidates.append(
            ("MusicBrainz Cover Art Archive", album_art["url"])
        )

    release_group_url = _musicbrainz_release_group_art_url(track)
    if release_group_url:
        candidates.append(
            ("MusicBrainz release-group artwork", release_group_url)
        )

    providers = [
        ("Apple iTunes", _itunes_artwork_url),
        ("Deezer", _deezer_artwork_url),
    ]

    for name, url in candidates:
        try:
            log(f"  Trying artwork: {name}")
            return _artwork_response(url), name
        except Exception as exc:
            log(f"  Artwork source failed: {name}: {exc}")

    for name, resolver in providers:
        try:
            log(f"  Trying artwork: {name}")
            url = resolver(track)
            if not url:
                raise RuntimeError("no exact album match")
            return _artwork_response(url), name
        except Exception as exc:
            log(f"  Artwork source failed: {name}: {exc}")

    log("  No artwork source succeeded; continuing without cover art.")
    return None, None


def publish_item(
    client,
    item,
    repo,
    repo_state,
):
    tracks = item["tracks"]

    audio_entries = []
    local_payloads = []
    first_data = metadata(tracks[0])

    log(
        f"Publishing {item['type']}: "
        f"{item_artist_name(item)} - {item_album_name(item)}"
    )

    log(f"  Size: {format_bytes(item_size(item))}")
    log(f"  Repository: {repo}")

    existing_paths = {
        normalize_path(entry.get("path", ""))
        for entry in repo_state.get("files", [])
    }

    # Fetch all payloads before creating any Git objects. Artwork is
    # best-effort: one provider failing must not abort the release.
    artwork_content, artwork_source = fetch_album_art(tracks[0])

    artist = sanitize_component(
        first_data.get("album_artist")
        or first_data.get("artist")
        or "Unknown Artist"
    )
    album = sanitize_component(
        first_data.get("album")
        or "Unknown Album"
    )
    artwork_path = normalize_path(
        f"{artist}/{album}/cover.jpg"
    )

    if artwork_content is not None:
        if artwork_path not in existing_paths:
            local_payloads.append(
                {
                    "path": artwork_path,
                    "content": artwork_content,
                    "kind": "art",
                }
            )
            log(f"  Artwork source: {artwork_source}")
        else:
            log(f"  Cover already exists; keeping existing artwork.")

    for track in tracks:
        local_file = get_local_file(track)
        if local_file is None:
            raise RuntimeError(
                "Downloaded file is missing "
                f"for {metadata(track).get('title')}"
            )

        local_size = local_file.stat().st_size
        if local_size > GITHUB_MAX_FILE_BYTES:
            raise RuntimeError(
                "Individual file exceeds GitHub's 100 MiB Git blob limit: "
                f"{relative_library_path(track)} "
                f"({format_bytes(local_size)})"
            )

        path = relative_library_path(track)
        local_payloads.append(
            {
                "path": path,
                "content": local_file.read_bytes(),
                "kind": "track",
                "track": track,
            }
        )

    log(f"  Creating atomic Git commit with {len(local_payloads)} file(s)...")

    tree_entries = []
    blob_info = []

    for payload in local_payloads:
        blob_sha = client.create_blob(
            repo,
            payload["content"],
        )
        tree_entries.append(
            {
                "path": payload["path"],
                "mode": "100644",
                "type": "blob",
                "sha": blob_sha,
            }
        )
        blob_info.append((payload, blob_sha))

    client.create_tree_commit(
        repo=repo,
        branch=repo_state["branch"],
        entries=tree_entries,
        message=(
            f"Add {item_artist_name(item)} - "
            f"{item_album_name(item)}"
        ),
    )

    for payload, blob_sha in blob_info:
        size = len(payload["content"])
        repo_state.setdefault("files", []).append(
            {
                "path": payload["path"],
                "size": size,
                "sha": blob_sha,
            }
        )
        repo_state["bytes"] += size

        if payload["kind"] == "track":
            mark_published(
                payload["track"],
                repo,
                payload["path"],
            )

    log(
        f"  Published {len(tracks)} track(s) "
        f"in one atomic commit."
    )
    log(
        f"  Repository usage: "
        f"{format_bytes(repo_state['bytes'])} / "
        f"{format_bytes(REPO_TARGET_BYTES)}"
    )
def item_fits_atomically(
    item,
    repo_state,
):
    size = item_size(
        item
    )

    return (
        size
        <= available_bytes(
            repo_state
        )
    )


def split_large_album(
    item,
    client,
    repos,
    repo_states,
):
    """
    Albums over 1 GiB are the sole exception to
    album atomicity.

    Tracks are placed individually using the same
    first-fitting-repository strategy.
    """

    log(
        f"Large album exception: "
        f"{item_artist_name(item)} - "
        f"{item_album_name(item)} "
        f"({format_bytes(item_size(item))})"
    )

    for track in item["tracks"]:
        track_size = get_download_size(
            track
        )

        placed = False

        for repo in list(repos):
            repo_state = repo_states[
                repo
            ]

            if (
                track_size
                <= available_bytes(
                    repo_state
                )
            ):
                single_item = {
                    "type": "track",
                    "first_index": 0,
                    "tracks": [track],
                }

                publish_item(
                    client,
                    single_item,
                    repo,
                    repo_state,
                )
                cleanup_duplicate_release(
                    client,
                    single_item,
                    repo,
                    repos,
                    repo_states,
                )

                placed = True
                break

        if not placed:
            repo = ensure_next_repo(
                client,
                repos,
                repo_states,
            )

            repo_state = repo_states[
                repo
            ]

            if track_size > GITHUB_MAX_FILE_BYTES:
                raise RuntimeError(
                    "Individual track exceeds GitHub's 100 MiB Git blob limit: "
                    f"{metadata(track).get('title')} "
                    f"({format_bytes(track_size)})"
                )

            if track_size > REPO_TARGET_BYTES:
                raise RuntimeError(
                    "Individual track exceeds "
                    f"repository target: "
                    f"{metadata(track).get('title')} "
                    f"({format_bytes(track_size)})"
                )

            single_item = {
                "type": "track",
                "first_index": 0,
                "tracks": [track],
            }

            publish_item(
                client,
                single_item,
                repo,
                repo_state,
            )


def publish():
    token = os.environ.get(
        "GIT_PAT"
    )

    if not token:
        raise SystemExit(
            "Missing GIT_PAT."
        )

    state = load_state()

    tracks = state.get(
        "tracks",
        [],
    )

    if RELEASE_FILTER:
        tracks = [track for track in tracks if release_key(track) == RELEASE_FILTER]

    items = build_items(
        tracks
    )

    if not items:
        log(
            "No downloaded tracks are "
            "ready for publishing."
        )
        return 0

    client = GitHubClient(
        token
    )

    log(
        f"GitHub library owner: "
        f"{client.owner}"
    )

    repos = discover_library_repos(
        client
    )

    repos = ensure_initial_repo(
        client,
        repos,
    )

    repo_states = {}

    log(
        "Scanning library repositories..."
    )

    for repo in repos:
        repo_states[repo] = (
            client.get_repo_state(
                repo
            )
        )

        log(
            f"  {repo}: "
            f"{format_bytes(repo_states[repo]['bytes'])} used, "
            f"{format_bytes(available_bytes(repo_states[repo]))} available"
        )

    log("")
    log(
        f"Publishing queue: "
        f"{len(items)} item(s)"
    )
    log(
        f"Normal repository target: "
        f"{format_bytes(REPO_TARGET_BYTES)}"
    )
    log(
        f"Atomic album limit: "
        f"{format_bytes(ALBUM_ATOMIC_LIMIT_BYTES)}"
    )
    log("")

    cleanup_partial_albums(
        client,
        state,
        repos,
        repo_states,
    )

    # Reuse existing library files before building the queue. This is
    # especially useful when two metadata IDs represent the same recording.
    if mark_existing_library_duplicates(tracks, repo_states):
        save_state(state)

    # Rebuild the state-derived publishing queue after cleanup/deduplication.
    items = build_items(
        tracks
    )

    pending_atomic = []

    # First pass:
    #
    # Walk the original acquisition order.
    # For each item, check every existing repository.
    #
    # If an atomic album doesn't fit anywhere, defer it
    # rather than immediately creating a new repository.
    for item in items:
        size = item_size(
            item
        )

        if size <= 0:
            log(
                f"Skipping item with no local "
                f"file data: "
                f"{item_artist_name(item)} - "
                f"{item_album_name(item)}"
            )
            continue

        if (
            item["type"] == "album"
            and size
            > ALBUM_ATOMIC_LIMIT_BYTES
        ):
            split_large_album(
                item,
                client,
                repos,
                repo_states,
            )

            save_state(
                state
            )

            continue

        placed = False

        for repo in list(repos):
            repo_state = repo_states[
                repo
            ]

            if item_fits_atomically(
                item,
                repo_state,
            ):
                publish_item(
                    client,
                    item,
                    repo,
                    repo_state,
                )
                cleanup_duplicate_release(
                    client,
                    item,
                    repo,
                    repos,
                    repo_states,
                )

                placed = True
                break

        if not placed:
            pending_atomic.append(
                item
            )

            log(
                f"Deferring item because "
                f"no current repository can "
                f"fit it: "
                f"{item_artist_name(item)} - "
                f"{item_album_name(item)} "
                f"({format_bytes(size)})"
            )

        save_state(
            state
        )

    # Second pass:
    #
    # Items that could not fit anywhere during
    # the first pass are now given new repositories.
    #
    # We still check every existing repository first,
    # because earlier placements may have changed
    # available capacity.
    for item in pending_atomic:
        size = item_size(
            item
        )

        placed = False

        for repo in list(repos):
            repo_state = repo_states[
                repo
            ]

            if item_fits_atomically(
                item,
                repo_state,
            ):
                publish_item(
                    client,
                    item,
                    repo,
                    repo_state,
                )

                placed = True
                break

        if placed:
            save_state(
                state
            )
            continue

        if size > REPO_TARGET_BYTES:
            # This should only be reachable for a
            # one-track item or an album that is
            # >900 MiB but <=1 GiB.
            #
            # Such an album is still atomic, so a
            # dedicated repository is allowed.
            log(
                f"Item exceeds the normal "
                f"repository target but is still "
                f"within the atomic album limit: "
                f"{format_bytes(size)}"
            )

        repo = ensure_next_repo(
            client,
            repos,
            repo_states,
        )

        repo_state = repo_states[
            repo
        ]

        if size > REPO_TARGET_BYTES:
            if (
                item["type"] == "album"
                and size
                <= ALBUM_ATOMIC_LIMIT_BYTES
            ):
                # The whole album goes into this
                # repository despite exceeding the
                # normal 900 MiB target.
                publish_item(
                    client,
                    item,
                    repo,
                    repo_state,
                )
                cleanup_duplicate_release(
                    client,
                    item,
                    repo,
                    repos,
                    repo_states,
                )
            else:
                raise RuntimeError(
                    "Atomic item cannot fit "
                    "within repository policy: "
                    f"{item_artist_name(item)} - "
                    f"{item_album_name(item)} "
                    f"({format_bytes(size)})"
                )
        else:
            publish_item(
                client,
                item,
                repo,
                repo_state,
            )
            cleanup_duplicate_release(
                client,
                item,
                repo,
                repos,
                repo_states,
            )

        save_state(
            state
        )

    log("")
    log(
        "Publishing complete."
    )

    log("")
    log(
        "Final library usage:"
    )

    for repo in repos:
        repo_state = repo_states[
            repo
        ]

        log(
            f"  {repo}: "
            f"{format_bytes(repo_state['bytes'])} / "
            f"{format_bytes(REPO_TARGET_BYTES)}"
        )

    save_state(
        state
    )

    return 0


def main():
    try:
        return publish()

    except KeyboardInterrupt:
        log(
            "Publishing interrupted."
        )
        return 130

    except Exception as exc:
        log(
            f"ERROR: {exc}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(
        main()
    )