import json
import subprocess
import sys
from pathlib import Path

from publish import GitHubClient, normalize_path

STATE_PATH = Path("state/tracks.json")
REMOVE_PATH = Path("remove.txt")


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


def load_requests():
    if not REMOVE_PATH.is_file():
        return []

    requests = []
    for raw in REMOVE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            requests.append(line.casefold())
    return requests


def album_key(track):
    metadata = track.get("metadata", {})
    album = str(metadata.get("album") or "").strip()
    artist = str(
        metadata.get("album_artist")
        or metadata.get("artist")
        or ""
    ).strip()
    return album.casefold(), artist.casefold()


def remove_from_library(client, repo, branch, paths, album_label):
    if not paths:
        return None

    repo_path = client._local_repo(repo)
    client._run_git(["fetch", "origin", branch], cwd=repo_path)
    client._run_git(["read-tree", f"origin/{branch}"], cwd=repo_path)

    # Remove every tracked file in the album directories, not just the audio
    # files recorded in state. This also catches OneTagger-created .lrc files.
    directories = sorted({
        str(Path(path).parent).replace("\\", "/")
        for path in paths
    })

    tracked = client._run_git(
        ["ls-tree", "-r", "--name-only", f"origin/{branch}"],
        cwd=repo_path,
    ).decode("utf-8", errors="replace").splitlines()

    deletions = []
    for path in tracked:
        normalized = normalize_path(path)
        if any(
            normalized == directory
            or normalized.startswith(directory + "/")
            for directory in directories
        ):
            client._run_git(
                ["rm", "--cached", "--ignore-unmatch", "--", normalized],
                cwd=repo_path,
            )
            deletions.append(normalized)

    if not deletions:
        return None

    client._run_git(
        [
            "-c", "user.name=MB-slsk-dl",
            "-c", "user.email=actions@users.noreply.github.com",
            "commit", "-m", f"Remove album: {album_label}",
        ],
        cwd=repo_path,
    )

    client._run_git(["push", "origin", f"HEAD:{branch}"], cwd=repo_path)

    return deletions


def main():
    requests = load_requests()
    if not requests:
        print("No album removal requests.")
        return 0

    state = load_state()
    tracks = state.get("tracks", [])
    client = GitHubClient(__import__("os").environ.get("GIT_PAT"))

    remove_keys = set(requests)
    matched = []
    kept = []

    for track in tracks:
        album, artist = album_key(track)
        if album in remove_keys:
            matched.append(track)
        else:
            kept.append(track)

    if not matched:
        print("No matching albums found in state.")
        return 0

    by_repo = {}
    for track in matched:
        library = track.get("acquisition", {}).get("library", {})
        repo = str(library.get("repo") or "").strip()
        path = normalize_path(library.get("path") or "")
        if repo and path:
            by_repo.setdefault(repo, []).append(path)

    removed_files = 0
    for repo, paths in by_repo.items():
        branch = client.get_repo_state(repo)["branch"]
        deletions = remove_from_library(
            client,
            repo,
            branch,
            paths,
            str(matched[0].get("metadata", {}).get("album") or "album"),
        )
        removed_files += len(deletions or [])

    removed_albums = sorted({
        str(t.get("metadata", {}).get("album") or "")
        for t in matched
    })

    state["tracks"] = kept
    save_state(state)

    subprocess.run(
        ["git", "config", "user.name", "github-actions[bot]"],
        check=True,
    )
    subprocess.run(
        [
            "git", "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        check=True,
    )
    subprocess.run(["git", "add", str(STATE_PATH)], check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)
    if staged.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "Remove requested albums from acquisition state"],
            check=True,
        )
        subprocess.run(["git", "push", "origin", "HEAD:main"], check=True)

    print(f"Removed albums: {', '.join(removed_albums)}")
    print(f"Removed library files: {removed_files}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nRemoval interrupted.", file=sys.stderr)
        raise
