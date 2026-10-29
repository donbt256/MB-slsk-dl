#!/usr/bin/env python3
"""Copy all rolling music-library repositories into Google Drive using rclone."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

import requests

API = "https://api.github.com"
PREFIX = os.environ.get("GITHUB_LIBRARY_PREFIX", "music-library-")
TIMEOUT = int(os.environ.get("GITHUB_REQUEST_TIMEOUT_SECONDS", "60"))


def github_session(token):
    if not token:
        raise RuntimeError("GIT_PAT is required to find and clone music-library repositories.")
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "MB-slsk-dl-rclone-backup",
    })
    return session


def get_library_repositories(session):
    user_response = session.get(f"{API}/user", timeout=TIMEOUT)
    user_response.raise_for_status()
    owner = user_response.json()["login"]

    repositories = []
    page = 1
    while True:
        response = session.get(
            f"{API}/user/repos",
            params={
                "per_page": 100,
                "page": page,
                "visibility": "all",
                "affiliation": "owner",
                "sort": "name",
                "direction": "asc",
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        for repo in batch:
            name = repo.get("name", "")
            if repo.get("owner", {}).get("login", "").casefold() != owner.casefold():
                continue
            match = re.fullmatch(re.escape(PREFIX) + r"(\d+)", name)
            if match:
                repositories.append((int(match.group(1)), name, repo["clone_url"]))
        if len(batch) < 100:
            break
        page += 1

    repositories.sort()
    return owner, repositories


def make_askpass(directory):
    askpass = directory / "git-askpass.sh"
    askpass.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  *Username*) printf "%s\\n" "x-access-token" ;;\n'
        '  *Password*) printf "%s\\n" "$GIT_PAT" ;;\n'
        '  *) exit 1 ;;\n'
        'esac\n',
        encoding="utf-8",
    )
    askpass.chmod(0o700)
    return askpass


def main():
    token = os.environ.get("GIT_PAT", "")
    session = github_session(token)
    owner, repositories = get_library_repositories(session)

    if not repositories:
        print(f"No repositories matching {PREFIX}<number> found for {owner}; nothing to upload.")
        return

    print(f"Found {len(repositories)} music-library repositories for {owner}.")

    with tempfile.TemporaryDirectory(prefix="mb-slsk-dl-drive-") as temp:
        temp_path = Path(temp)
        askpass = make_askpass(temp_path)
        env = os.environ.copy()
        env.update({
            "GIT_ASKPASS": str(askpass),
            "GIT_TERMINAL_PROMPT": "0",
        })

        for number, name, clone_url in repositories:
            checkout = temp_path / name
            print(f"Cloning {name}...")
            subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", clone_url, str(checkout)],
                check=True,
                env=env,
                stdout=subprocess.DEVNULL,
            )
            print(f"Copying {name} into Google Drive Music/...")
            subprocess.run(
                [
                    "rclone", "copy", str(checkout), "gdrive:Music",
                    "--exclude", ".git/**",
                    "--exclude", "/README.md",
                    "--exclude", "/.github/**",
                    "--transfers", "4",
                    "--checkers", "8",
                    "--fast-list",
                ],
                check=True,
            )

    print("Google Drive music-library copy completed.")


if __name__ == "__main__":
    main()
