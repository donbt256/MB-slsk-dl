import hashlib
import os
import re
from difflib import SequenceMatcher
from pathlib import PurePosixPath, PureWindowsPath


MATCHER_VERSION = 8


MAX_LIBRARY_FILE_BYTES = 100 * 1024 * 1024


AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".wav",
    ".alac",
    ".ape",
    ".wv",
    ".aiff",
    ".aif",
}


ALTERNATE_TERMS = {
    "live",
    "acoustic",
    "demo",
    "remix",
    "instrumental",
    "karaoke",
    "edit",
    "radio edit",
    "piano",
    "rehearsal",
    "session",
    "sessions",
    "cover",
    "tribute",
    "bootleg",
}


def normalize(value):
    value = str(value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[\[\]{}()]", " ", value)
    value = re.sub(r"[_\-]+", " ", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tokens(value):
    return set(normalize(value).split())


def primary_artist(metadata, album=False):
    """
    Return the primary credited artist when MusicBrainz supplied the
    individual artist list. Multi-artist credits otherwise make a
    Soulseek path match unnecessarily strict.
    """
    keys = (
        ("album_artists", "album_artist")
        if album
        else ("artists", "artist")
    )

    for key in keys:
        value = metadata.get(key)

        if isinstance(value, list):
            for item in value:
                item = str(item or "").strip()
                if item:
                    return item

        value = str(value or "").strip()
        if value:
            return value

    return ""


def similarity(a, b):
    a = normalize(a)
    b = normalize(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    return SequenceMatcher(None, a, b).ratio()


def token_similarity(a, b):
    ta = tokens(a)
    tb = tokens(b)

    if not ta or not tb:
        return 0.0

    return len(ta & tb) / max(len(ta | tb), 1)


def has_audio_extension(filename):
    _, extension = os.path.splitext(str(filename or ""))
    return extension.lower() in AUDIO_EXTENSIONS


def split_path(filename):
    filename = str(filename or "")

    normalized = filename.replace("\\", "/")

    parts = [
        part
        for part in PurePosixPath(normalized).parts
        if part not in {"", "."}
    ]

    return parts


def parent_path(filename):
    parts = split_path(filename)

    if len(parts) <= 1:
        return ""

    return "/".join(parts[:-1])


def path_parts_without_filename(filename):
    return split_path(filename)[:-1]


def filename_without_extension(filename):
    name = (
        split_path(filename)[-1]
        if split_path(filename)
        else str(filename or "")
    )

    return os.path.splitext(name)[0]


def extract_track_number(filename):
    name = filename_without_extension(filename)

    patterns = [
        r"^\s*(\d{1,3})\s*[-._ ]",
        r"^\s*(\d{1,3})\s+",
        r"\btrack\s*(\d{1,3})\b",
        r"\bdisc\s*\d+\s*[-._ ]\s*(\d{1,3})\b",
        r"\b\d{1,2}[-_.](\d{1,3})\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            name,
            re.IGNORECASE,
        )

        if match:
            try:
                return int(match.group(1))
            except ValueError:
                pass

    return None


def extract_candidate_title(filename, artist=None):
    title = filename_without_extension(filename)

    # Remove a leading track number.
    title = re.sub(
        r"^\s*(?:disc\s*\d+\s*[-._ ]\s*)?\d{1,3}\s*[-._ ]\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # Soulseek users sometimes prefix tracks with elapsed-time-style
    # numbers, e.g. "01:01 - I Am Trying to Break Your Heart.flac".
    # These are not track numbers; strip them before title matching.
    title = re.sub(
        r"^\s*\d{1,3}:\d{2}(?::\d{2})?\s*[-._ ]\s*",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # Remove common "Artist - Title" formatting.
    if artist:
        normalized_artist = normalize(artist)
        normalized_title = normalize(title)

        if normalized_title.startswith(
            normalized_artist + " "
        ):
            raw_artist = title[
                : len(title)
                - len(
                    normalized_title[
                        len(normalized_artist) + 1 :
                    ]
                )
            ]

            # More reliable fallback for normal "Artist - Title" paths.
            separator_match = re.match(
                r"^\s*(.*?)\s*[-–—]\s*(.+)$",
                title,
            )

            if separator_match:
                left = normalize(
                    separator_match.group(1)
                )

                if similarity(left, artist) >= 0.75:
                    title = separator_match.group(2)

    # General artist-title separator.
    separator_match = re.match(
        r"^\s*(.*?)\s*[-–—]\s*(.+)$",
        title,
    )

    if separator_match and artist:
        left = separator_match.group(1)
        right = separator_match.group(2)

        if similarity(left, artist) >= 0.80:
            title = right

    return title.strip()


def alternate_terms_in(value):
    normalized = normalize(value)
    found = []

    for term in ALTERNATE_TERMS:
        if term in normalized:
            found.append(term)

    return sorted(found)


def candidate_id(candidate):
    identity = "\x1f".join(
        [
            str(candidate.get("username") or ""),
            str(candidate.get("filename") or ""),
            str(candidate.get("size") or ""),
            str(candidate.get("extension") or ""),
        ]
    )

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()[:16]


def release_id(username, folder):
    identity = f"{username}\x1f{folder}"

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()[:16]


def score_candidate(track, candidate):
    metadata = track.get("metadata", track)

    artist = metadata.get("artist", "")
    primary = primary_artist(metadata)
    album = metadata.get("album", "")
    title = metadata.get("title", "")
    track_number = metadata.get("track_number")

    filename = candidate.get("filename", "")
    username = candidate.get("username", "")

    if not has_audio_extension(filename):
        return None

    size = candidate.get("size")
    if size is not None:
        try:
            if int(size) > MAX_LIBRARY_FILE_BYTES:
                return None
        except (TypeError, ValueError):
            pass

    candidate_title = extract_candidate_title(
        filename,
        artist,
    )

    title_exact = (
        normalize(candidate_title)
        == normalize(title)
    )

    title_similarity = similarity(
        candidate_title,
        title,
    )

    artist_path_similarity = 0.0
    album_path_similarity = 0.0

    path_parts = path_parts_without_filename(
        filename
    )

    for part in path_parts:
        artist_path_similarity = max(
            artist_path_similarity,
            similarity(part, artist),
            token_similarity(part, artist),
            similarity(part, primary),
            token_similarity(part, primary),
        )

        album_path_similarity = max(
            album_path_similarity,
            similarity(part, album),
            token_similarity(part, album),
        )

    candidate_track_number = extract_track_number(
        filename
    )

    score = 0.0

    if title_exact:
        score += 55.0
    else:
        score += 45.0 * title_similarity

    score += 18.0 * artist_path_similarity
    score += 17.0 * album_path_similarity

    if (
        track_number is not None
        and candidate_track_number is not None
    ):
        if int(track_number) == candidate_track_number:
            score += 10.0
        elif (
            abs(
                int(track_number)
                - candidate_track_number
            )
            == 1
        ):
            score += 3.0

    alternate_terms = alternate_terms_in(
        f"{filename} {' '.join(path_parts)}"
    )

    # These are only penalized when the metadata target itself does not
    # contain the corresponding version terminology.
    target_terms = alternate_terms_in(
        f"{title} {album}"
    )

    unexpected_alternates = [
        term
        for term in alternate_terms
        if term not in target_terms
    ]

    score -= min(
        30.0,
        len(unexpected_alternates) * 12.0,
    )

    score = max(
        0.0,
        min(100.0, score),
    )

    # Deterministic matching has no fallback model. Alternate-version terms
    # are hard exclusions unless they are explicitly present in the target.
    if unexpected_alternates:
        decision = "reject"
    elif title_exact and artist_path_similarity >= 0.75:
        decision = "accept"
    elif score >= 78.0 and title_similarity >= 0.80:
        decision = "accept"
    else:
        decision = "reject"

    result = dict(candidate)

    result["candidate_id"] = candidate_id(
        candidate
    )
    result["score"] = round(score, 2)
    result["decision"] = decision
    result["candidate_title"] = candidate_title
    result["candidate_track_number"] = (
        candidate_track_number
    )
    result["alternate_terms"] = (
        unexpected_alternates
    )
    result["title_similarity"] = round(
        title_similarity,
        4,
    )
    result["artist_path_similarity"] = round(
        artist_path_similarity,
        4,
    )
    result["album_path_similarity"] = round(
        album_path_similarity,
        4,
    )

    return result


def rank_candidates(track, candidates):
    scored = []

    for candidate in candidates:
        result = score_candidate(
            track,
            candidate,
        )

        if result is not None:
            scored.append(result)

    scored.sort(
        key=lambda item: (
            item["score"],
            item.get("title_similarity", 0),
            item.get("artist_path_similarity", 0),
            item.get("album_path_similarity", 0),
        ),
        reverse=True,
    )

    return scored


def group_release_candidates(candidates):
    groups = {}

    for candidate in candidates:
        filename = candidate.get("filename", "")

        if not has_audio_extension(filename):
            continue

        size = candidate.get("size")
        if size is not None:
            try:
                if int(size) > MAX_LIBRARY_FILE_BYTES:
                    continue
            except (TypeError, ValueError):
                pass

        username = str(
            candidate.get("username") or ""
        )

        folder = parent_path(filename)

        key = (
            username,
            folder,
        )

        if key not in groups:
            groups[key] = {
                "username": username,
                "folder": folder,
                "files": [],
            }

        groups[key]["files"].append(candidate)

    releases = []

    for group in groups.values():
        group["release_id"] = release_id(
            group["username"],
            group["folder"],
        )

        releases.append(group)

    return releases


def score_release(album_tracks, release):
    files = release["files"]

    if not files:
        return None

    first_metadata = album_tracks[0]["metadata"]

    artist = first_metadata.get(
        "artist",
        "",
    )

    primary = primary_artist(
        first_metadata,
        album=True,
    )

    album = first_metadata.get(
        "album",
        "",
    )

    folder = release.get(
        "folder",
        "",
    )

    artist_similarity = max(
        similarity(folder, artist),
        similarity(folder, primary),
    )

    album_similarity = similarity(
        folder,
        album,
    )

    normalized_files = []

    for file_info in files:
        filename = file_info.get(
            "filename",
            "",
        )

        if not has_audio_extension(filename):
            continue

        normalized_files.append(
            file_info
        )

    if not normalized_files:
        return None

    matches = []
    used_ids = set()

    for track in album_tracks:
        ranked = rank_candidates(
            track,
            normalized_files,
        )

        best = None

        for candidate in ranked:
            cid = candidate["candidate_id"]

            if cid in used_ids:
                continue

            best = candidate
            break

        if best is not None:
            used_ids.add(
                best["candidate_id"]
            )

            matches.append(
                {
                    "track_id": track["metadata"].get(
                        "id"
                    ),
                    "title": track["metadata"].get(
                        "title"
                    ),
                    "track_number": track["metadata"].get(
                        "track_number"
                    ),
                    "candidate": best,
                }
            )

    expected = len(album_tracks)
    matched = len(matches)

    coverage = (
        matched / expected
        if expected
        else 0.0
    )

    exact_titles = sum(
        1
        for match in matches
        if normalize(
            match["candidate"].get(
                "candidate_title",
                "",
            )
        )
        == normalize(
            match.get("title", "")
        )
    )

    exact_track_numbers = sum(
        1
        for match in matches
        if (
            match["candidate"].get(
                "candidate_track_number"
            )
            is not None
            and match.get(
                "track_number"
            )
            is not None
            and int(
                match["candidate"][
                    "candidate_track_number"
                ]
            )
            == int(
                match["track_number"]
            )
        )
    )

    average_track_score = (
        sum(
            match["candidate"]["score"]
            for match in matches
        )
        / matched
        if matched
        else 0.0
    )

    alternate_count = sum(
        bool(
            match["candidate"].get(
                "alternate_terms"
            )
        )
        for match in matches
    )

    score = (
        coverage * 55.0
        + (
            exact_titles / expected
            if expected
            else 0.0
        ) * 20.0
        + (
            exact_track_numbers / expected
            if expected
            else 0.0
        ) * 10.0
        + (
            average_track_score / 100.0
        ) * 10.0
        + album_similarity * 3.0
        + artist_similarity * 2.0
    )

    score -= min(
        15.0,
        alternate_count * 2.0,
    )

    score = max(
        0.0,
        min(100.0, score),
    )

    exact_title_ratio = (
        exact_titles / expected
        if expected
        else 0.0
    )

    # Album acceptance requires a complete, internally consistent tracklist.
    if (
        matched == expected
        and coverage == 1.0
        and alternate_count == 0
        and exact_title_ratio >= 0.90
        and score >= 70.0
    ):
        decision = "accept"
    else:
        decision = "reject"

    return {
        "release_id": release["release_id"],
        "username": release["username"],
        "folder": release["folder"],
        "file_count": len(normalized_files),
        "expected_tracks": expected,
        "matched_tracks": matched,
        "coverage": round(
            coverage,
            4,
        ),
        "exact_titles": exact_titles,
        "exact_track_numbers": (
            exact_track_numbers
        ),
        "average_track_score": round(
            average_track_score,
            2,
        ),
        "album_similarity": round(
            album_similarity,
            4,
        ),
        "artist_similarity": round(
            artist_similarity,
            4,
        ),
        "alternate_count": alternate_count,
        "score": round(
            score,
            2,
        ),
        "decision": decision,
        "matches": matches,
    }


def rank_releases(
    album_tracks,
    candidates,
):
    releases = group_release_candidates(
        candidates
    )

    scored = []

    for release in releases:
        result = score_release(
            album_tracks,
            release,
        )

        if result is not None:
            scored.append(result)

    scored.sort(
        key=lambda item: (
            item["score"],
            item["coverage"],
            item["matched_tracks"],
            item["exact_titles"],
        ),
        reverse=True,
    )

    return scored


def classify_candidates(scored):
    if not scored:
        return {
            "decision": "reject",
            "candidates": [],
        }

    if scored[0].get("decision") == "accept":
        return {
            "decision": "accept",
            "candidates": scored,
        }

    return {
        "decision": "reject",
        "candidates": scored,
    }
