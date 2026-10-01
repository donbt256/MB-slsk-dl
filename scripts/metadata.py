import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

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


def checkout_files(client, repo, branch, paths):
    path = local_repo_path(client, repo)
    client._run_git(["checkout", "--force", branch], cwd=path)
    client._run_git(["reset", "--hard", f"origin/{branch}"], cwd=path)

    for relative in sorted(paths):
        relative = normalize_path(relative)
        client._run_git(
            ["checkout", f"origin/{branch}", "--", relative],
            cwd=path,
        )

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
    result = subprocess.run(
        command,
        cwd=path,
        check=False,
    )

    return result.returncode


def process_repo(client, repo, branch, track_entries):
    paths = {
        normalize_path(entry["path"])
        for entry in track_entries
        if entry.get("path")
    }

    if not paths:
        return {
            "repo": repo,
            "changed": [],
            "success": True,
            "commit": None,
        }

    log("")
    log(f"=== Metadata: {repo} ===")

    repo_path = checkout_files(
        client,
        repo,
        branch,
        paths,
    )

    # Only the files belonging to this metadata run are checked out. Group
    # them by directory so OneTagger can perform album-level matching without
    # accidentally processing the entire library repository.
    directories = defaultdict(list)

    for relative in sorted(paths):
        file_path = repo_path / relative
        if not file_path.is_file():
            log(f"Missing library file: {relative}")
            continue
        directories[file_path.parent].append(file_path)

    failed = False

    for directory, files in sorted(
        directories.items(),
        key=lambda item: str(item[0]),
    ):
        relative_directory = directory.relative_to(repo_path)

        log("")
        log(
            f"Tagging {len(files)} file(s) in "
            f"{relative_directory}"
        )

        for index, file_path in enumerate(files, 1):
            log(
                f"  [{index}/{len(files)}] "
                f"{file_path.relative_to(repo_path)}"
            )

        log("  Starting OneTagger...")
        return_code = run_onetagger(directory)

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

        if return_code != 0:
            failed = True
            log(
                f"OneTagger failed for "
                f"{directory.relative_to(repo_path)}"
            )

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
        }

    entries = []
    changed_paths = []

    for relative in changed:
        normalized = normalize_path(relative)
        local = repo_path / normalized

        if not local.is_file():
            # Do not accidentally commit deletions created by a failed
            # tagger invocation. Metadata processing only updates existing
            # library files and optional LRC sidecars.
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
    }


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

        if not result["success"]:
            overall_success = False

        changed = set(result["changed"])

        for entry in entries:
            track = entry["track"]
            path = entry["path"]
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
    subprocess.run(["git", "add", str(STATE_PATH)], check=True)

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
            f"status={'ok' if result['success'] else 'failed'}"
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
