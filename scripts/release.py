import hashlib


def metadata(track):
    return track.get("metadata", track)


def release_identity(track):
    data = metadata(track)
    artist = str(
        data.get("album_artist")
        or data.get("artist")
        or ""
    ).strip()
    album = str(data.get("album") or "").strip()
    return artist, album


def release_key(track):
    sources = track.get("sources", {})
    release_ids = sources.get("release_ids", [])

    if release_ids:
        artist, album = release_identity(track)
        raw = f"release:{release_ids[0]}\x1f{artist}\x1f{album}".encode(
            "utf-8", errors="replace"
        )
    else:
        track_id = metadata(track).get("id") or ""
        raw = f"track:{track_id}".encode("utf-8", errors="replace")

    return hashlib.sha1(raw).hexdigest()[:16]


def release_label(track):
    data = metadata(track)
    sources = track.get("sources", {})
    if sources.get("release_ids"):
        artist, album = release_identity(track)
        return f"{artist} - {album}"

    return (
        f"{data.get('artist') or 'Unknown Artist'} - "
        f"{data.get('title') or 'Unknown Track'}"
    )


def group_releases(tracks):
    groups = {}

    for track in tracks:
        if not track.get("active_source", True):
            continue

        key = release_key(track)

        if key not in groups:
            groups[key] = {
                "key": key,
                "label": release_label(track),
                "tracks": [],
            }

        groups[key]["tracks"].append(track)

    return list(groups.values())
