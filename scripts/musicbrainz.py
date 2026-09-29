import re
import time
from urllib.parse import quote

import requests


MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_USER_AGENT = (
    "MB-slsk-dl/1.0 "
    "(https://github.com/donbt256/MB-slsk-dl)"
)
REQUEST_INTERVAL_SECONDS = 1.5


class MusicBrainzClient:
    def __init__(self):
        self._next_request_at = 0.0

    def _request(self, endpoint, params=None):
        max_attempts = 5

        for attempt in range(1, max_attempts + 1):
            wait = self._next_request_at - time.monotonic()
            if wait > 0:
                time.sleep(wait)

            try:
                response = requests.get(
                    f"{MUSICBRAINZ_API_URL}{endpoint}",
                    params=params,
                    headers={
                        "User-Agent": MUSICBRAINZ_USER_AGENT,
                        "Accept": "application/json",
                    },
                    timeout=30,
                )
            except requests.RequestException:
                if attempt == max_attempts:
                    raise

                delay = min(2 ** (attempt - 1), 16)
                print(
                    f"  MusicBrainz request failed; retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_attempts})",
                    flush=True,
                )
                self._next_request_at = time.monotonic() + delay
                continue

            self._next_request_at = (
                time.monotonic() + REQUEST_INTERVAL_SECONDS
            )

            if response.status_code in {429, 500, 502, 503, 504}:
                if attempt == max_attempts:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After")
                try:
                    delay = max(1, int(retry_after)) if retry_after else 2 ** (attempt - 1)
                except ValueError:
                    delay = 2 ** (attempt - 1)

                delay = min(delay, 30)

                print(
                    f"  MusicBrainz returned HTTP {response.status_code}; "
                    f"retrying in {delay}s "
                    f"(attempt {attempt + 1}/{max_attempts})",
                    flush=True,
                )
                self._next_request_at = time.monotonic() + delay
                continue

            response.raise_for_status()
            return response.json()

        raise RuntimeError("MusicBrainz request retry loop exhausted.")

    @staticmethod
    def parse_url(url):
        match = re.search(
            r"musicbrainz\.org/(release|release-group)/"
            r"([0-9a-fA-F-]{36})",
            url,
        )
        if not match:
            raise ValueError(f"Unsupported MusicBrainz URL: {url}")
        return match.group(1), match.group(2)

    @staticmethod
    def _artist_credit(credits):
        names = []
        for credit in credits or []:
            artist = credit.get("artist", {})
            name = credit.get("name") or artist.get("name")
            if name:
                names.append(name)
            join = credit.get("joinphrase", "")
            if join:
                names.append(join)
        return "".join(names).strip()

    @staticmethod
    def _track_number(value):
        match = re.search(r"\d+", str(value or ""))
        return int(match.group()) if match else None

    def get_release(self, release_id):
        release = self._request(
            f"/release/{release_id}",
            params={
                "inc": "artist-credits+media+recordings+isrcs",
                "fmt": "json",
            },
        )
        return self.normalize_release(release)

    def normalize_release(self, release):
        release_id = release["id"]
        album = release.get("title")
        album_artists = self._artist_credit(
            release.get("artist-credit", [])
        )
        release_date = release.get("date")
        media = release.get("media", [])
        total_tracks = sum(
            len(medium.get("tracks", []))
            for medium in media
        )

        tracks = []
        for medium in media:
            disc_number = medium.get("position")
            for track in medium.get("tracks", []):
                recording = track.get("recording", {})
                recording_artists = self._artist_credit(
                    track.get("artist-credit")
                    or recording.get("artist-credit")
                    or release.get("artist-credit", [])
                )
                artist = recording_artists or album_artists
                title = track.get("title") or recording.get("title")
                isrcs = (
                    recording.get("isrcs")
                    or [
                        item.get("id")
                        for item in recording.get("isrc-list", [])
                        if item.get("id")
                    ]
                )

                tracks.append(
                    {
                        "id": track.get("id")
                        or recording.get("id")
                        or f"{release_id}:{medium.get('position')}:{track.get('position')}",
                        "url": f"https://musicbrainz.org/release/{release_id}",
                        "artist": artist,
                        "artists": [
                            credit.get("name")
                            or credit.get("artist", {}).get("name")
                            for credit in (
                                track.get("artist-credit")
                                or recording.get("artist-credit")
                                or release.get("artist-credit", [])
                            )
                            if (
                                credit.get("name")
                                or credit.get("artist", {}).get("name")
                            )
                        ],
                        "album": album,
                        "album_artist": album_artists,
                        "album_artists": [
                            credit.get("name")
                            or credit.get("artist", {}).get("name")
                            for credit in release.get("artist-credit", [])
                            if (
                                credit.get("name")
                                or credit.get("artist", {}).get("name")
                            )
                        ],
                        "title": title,
                        "track_number": self._track_number(
                            track.get("number")
                        ),
                        "disc_number": disc_number,
                        "total_tracks": total_tracks,
                        "duration_ms": (
                            track.get("length")
                            or recording.get("length")
                        ),
                        "release_date": release_date,
                        "release_date_precision": (
                            "day" if release_date and len(release_date) == 10
                            else "month" if release_date and len(release_date) == 7
                            else "year" if release_date
                            else None
                        ),
                        "isrc": isrcs[0] if isrcs else None,
                        "album_art": {
                            "url": (
                                f"https://coverartarchive.org/release/"
                                f"{release_id}/front-500"
                            ),
                            "width": 500,
                            "height": 500,
                        },
                        "musicbrainz_release_id": release_id,
                        "musicbrainz_recording_id": recording.get("id"),
                    }
                )

        return {
            "release": {
                "id": release_id,
                "url": f"https://musicbrainz.org/release/{release_id}",
                "name": album,
                "artist": album_artists,
                "release_date": release_date,
                "status": release.get("status"),
                "country": release.get("country"),
                "release_group": (
                    release.get("release-group", {}).get("id")
                ),
            },
            "tracks": tracks,
        }

    def search_release(self, artist, album):
        query = f'artist:"{artist}" AND release:"{album}"'
        data = self._request(
            "/release",
            params={
                "query": query,
                "limit": 25,
                "fmt": "json",
            },
        )
        results = data.get("releases", [])

        # Streaming services often append edition labels that MusicBrainz
        # does not use in the release-group title. If the exact release
        # search fails, progressively normalize the requested title and
        # search for its release group.
        if not results:
            base_album = re.sub(
                r"\s*\([^)]*\)\s*$",
                "",
                album,
            ).strip()

            base_album = re.sub(
                r"\s+[-–—]\s+(?:deluxe|expanded|anniversary|remaster|"
                r"remastered|special|edition)\b.*$",
                "",
                base_album,
                flags=re.IGNORECASE,
            ).strip()

            if base_album and base_album.casefold() != album.casefold():
                group_data = self._request(
                    "/release-group",
                    params={
                        "query": (
                            f'artist:"{artist}" AND '
                            f'releasegroup:"{base_album}"'
                        ),
                        "limit": 25,
                        "fmt": "json",
                    },
                )
                groups = group_data.get("release-groups", [])

                artist_norm = artist.casefold().strip()
                base_norm = base_album.casefold().strip()

                def group_score(item):
                    item_artist = self._artist_credit(
                        item.get("artist-credit", [])
                    ).casefold().strip()
                    item_title = item.get("title", "").casefold().strip()
                    value = 0
                    if item_artist == artist_norm:
                        value += 100
                    if item_title == base_norm:
                        value += 100
                    if str(item.get("primary-type", "")).casefold() == "album":
                        value += 10
                    return value

                if groups:
                    groups = sorted(
                        groups,
                        key=group_score,
                        reverse=True,
                    )

                    for group in groups:
                        try:
                            resolved = self.get_release_group(group["id"])
                        except ValueError as exc:
                            if "has no official releases" not in str(exc):
                                raise
                            print(
                                f"  Skipping MusicBrainz release group "
                                f"{group['id']}: no official releases.",
                                flush=True,
                            )
                            continue

                        print(
                            f"  MusicBrainz release-group match: "
                            f"{self._artist_credit(group.get('artist-credit', []))} - "
                            f"{group.get('title')} "
                            f"({group['id']})",
                            flush=True,
                        )
                        return resolved

        if not results:
            base_album = re.sub(
                r"\s*\((?:deluxe|expanded|anniversary|remaster|remastered)"
                r"(?:[^)]*)\)\s*$",
                "",
                album,
                flags=re.IGNORECASE,
            ).strip()

            if base_album and base_album.casefold() != album.casefold():
                group_data = self._request(
                    "/release-group",
                    params={
                        "query": (
                            f'artist:"{artist}" AND '
                            f'releasegroup:"{base_album}"'
                        ),
                        "limit": 25,
                        "fmt": "json",
                    },
                )
                groups = group_data.get("release-groups", [])

                artist_norm = artist.casefold().strip()
                base_norm = base_album.casefold().strip()

                def group_score(item):
                    item_artist = self._artist_credit(
                        item.get("artist-credit", [])
                    ).casefold().strip()
                    item_title = item.get("title", "").casefold().strip()
                    value = 0
                    if item_artist == artist_norm:
                        value += 100
                    if item_title == base_norm:
                        value += 100
                    if str(item.get("primary-type", "")).casefold() == "album":
                        value += 10
                    return value

                if groups:
                    groups = sorted(
                        groups,
                        key=group_score,
                        reverse=True,
                    )

                    for group in groups:
                        try:
                            resolved = self.get_release_group(group["id"])
                        except ValueError as exc:
                            if "has no official releases" not in str(exc):
                                raise
                            print(
                                f"  Skipping MusicBrainz release group "
                                f"{group['id']}: no official releases.",
                                flush=True,
                            )
                            continue

                        print(
                            f"  MusicBrainz release-group match: "
                            f"{self._artist_credit(group.get('artist-credit', []))} - "
                            f"{group.get('title')} "
                            f"({group['id']})",
                            flush=True,
                        )
                        return resolved

        if not results:
            raise ValueError(
                f"MusicBrainz could not find a release for "
                f"{artist} - {album}."
            )

        artist_norm = artist.casefold().strip()
        album_norm = album.casefold().strip()

        def score(item):
            item_artist = self._artist_credit(
                item.get("artist-credit", [])
            ).casefold().strip()
            item_title = item.get("title", "").casefold().strip()
            release_group = item.get("release-group", {})
            primary_type = str(
                release_group.get("primary-type", "")
            ).casefold()
            status = str(item.get("status", "")).casefold()

            value = 0
            if item_artist == artist_norm:
                value += 100
            if item_title == album_norm:
                value += 100
            if status == "official":
                value += 20
            if primary_type in {"album", "ep", "single"}:
                value += 10
            return value

        best = max(results, key=score)
        print(
            f"  MusicBrainz match: "
            f"{self._artist_credit(best.get('artist-credit', []))} - "
            f"{best.get('title')} "
            f"({best.get('id')})",
            flush=True,
        )
        return self.get_release(best["id"])

    def get_release_group(self, release_group_id):
        data = self._request(
            "/release",
            params={
                "release-group": release_group_id,
                "status": "official",
                "limit": 100,
                "fmt": "json",
            },
        )
        releases = data.get("releases", [])
        if not releases:
            raise ValueError(
                f"MusicBrainz release group {release_group_id} "
                "has no official releases."
            )

        # Prefer a digital release, then a worldwide release, then the
        # first official release returned by MusicBrainz.
        def score(item):
            value = 0
            if str(item.get("country", "")).upper() == "XW":
                value += 20
            for medium in item.get("media", []):
                if str(medium.get("format", "")).casefold() == "digital media":
                    value += 10
            return value

        best = max(releases, key=score)
        return self.get_release(best["id"])


def _merge_resolved(target, resolved, requested_track_ids=None):
    release = resolved["release"]
    release_id = release["id"]

    if not any(item.get("id") == release_id for item in target["releases"]):
        target["releases"].append(release)

    allowed = set(requested_track_ids) if requested_track_ids is not None else None

    for track in resolved["tracks"]:
        track_id = track["id"]
        if allowed is not None and track_id not in allowed:
            continue

        target["tracks"][track_id] = track
        entry = target["track_sources"].setdefault(
            track_id,
            {"release_ids": [], "track_ids": []},
        )
        if release_id not in entry["release_ids"]:
            entry["release_ids"].append(release_id)


def resolve_requests(requests, cache=None):
    client = MusicBrainzClient()

    cache = cache if isinstance(cache, dict) else {}
    cache.setdefault("releases", {})
    cache.setdefault("release_groups", {})
    cache.setdefault("searches", {})

    result = {
        "tracks": {},
        "track_sources": {},
        "releases": [],
        "unresolved": [],
        "cache": cache,
    }

    album_requests = requests.albums
    track_requests = requests.tracks

    print(
        f"Found {len(album_requests)} album request(s) and "
        f"{len(track_requests)} track request(s).",
        flush=True,
    )

    def resolve_album(artist, album):
        cache_key = f"{artist}\x1f{album}"
        cached = cache["searches"].get(cache_key)

        if cached:
            print(
                f"  Using cached MusicBrainz search: "
                f"{artist} - {album}",
                flush=True,
            )
            return cached

        resolved = client.search_release(artist, album)
        cache["searches"][cache_key] = resolved
        return resolved

    for request in album_requests:
        print(
            f"Resolving album: {request.artist} - {request.album}",
            flush=True,
        )
        try:
            resolved = resolve_album(request.artist, request.album)
        except ValueError as exc:
            print(
                f"  MusicBrainz could not resolve this request; "
                f"skipping: {exc}",
                flush=True,
            )
            result["unresolved"].append(
                {
                    "artist": request.artist,
                    "album": request.album,
                    "error": str(exc),
                }
            )
            continue

        _merge_resolved(result, resolved)

    for request in track_requests:
        print(
            f"Resolving track: "
            f"{request.artist} - {request.album} - {request.title}",
            flush=True,
        )
        resolved = resolve_album(request.artist, request.album)

        title_norm = request.title.casefold().strip()
        matching_ids = {
            track["id"]
            for track in resolved["tracks"]
            if str(track.get("title") or "").casefold().strip()
            == title_norm
        }

        if not matching_ids:
            raise ValueError(
                f"MusicBrainz found the album "
                f"{request.artist} - {request.album}, but no track "
                f"named {request.title!r}."
            )

        _merge_resolved(
            result,
            resolved,
            requested_track_ids=matching_ids,
        )

    return {
        "tracks": list(result["tracks"].values()),
        "track_sources": result["track_sources"],
        "releases": result["releases"],
        "unresolved": result["unresolved"],
        "cache": result["cache"],
    }


def resolve_urls(urls, cache=None):
    client = MusicBrainzClient()
    tracks = {}
    track_sources = {}
    releases = []
    cache = cache if isinstance(cache, dict) else {}
    cache.setdefault("releases", {})
    cache.setdefault("release_groups", {})
    cache.setdefault("searches", {})

    for raw_url in urls:
        value = raw_url.strip()
        if not value or value.startswith("#"):
            continue

        kind = None
        entity_id = None
        if "musicbrainz.org/" in value:
            kind, entity_id = client.parse_url(value.split("?", 1)[0])
            print(
                f"Resolving MusicBrainz {kind}: {entity_id}",
                flush=True,
            )
        else:
            if " - " not in value:
                raise ValueError(
                    "Input must be a MusicBrainz release URL, "
                    "release-group URL, or 'Artist - Album': "
                    f"{value}"
                )
            artist, album = value.split(" - ", 1)
            cache_key = f"{artist.strip()}\x1f{album.strip()}"
            cached = cache["searches"].get(cache_key)
            if cached:
                print(
                    f"  Using cached MusicBrainz search: {artist} - {album}",
                    flush=True,
                )
                resolved = cached
            else:
                resolved = client.search_release(
                    artist.strip(),
                    album.strip(),
                )
                cache["searches"][cache_key] = resolved
            release = resolved["release"]
            entity_id = release["id"]
            kind = "release"

        if kind == "release":
            cached = cache["releases"].get(entity_id)
            if cached:
                resolved = cached
                print("  Using cached MusicBrainz release.", flush=True)
            else:
                resolved = client.get_release(entity_id)
                cache["releases"][entity_id] = resolved
        elif kind == "release-group":
            cached = cache["release_groups"].get(entity_id)
            if cached:
                resolved = cached
                print("  Using cached MusicBrainz release group.", flush=True)
            else:
                resolved = client.get_release_group(entity_id)
                cache["release_groups"][entity_id] = resolved
                cache["releases"][resolved["release"]["id"]] = resolved
        else:
            raise ValueError(f"Unsupported MusicBrainz type: {kind}")

        release = resolved["release"]
        release_id = release["id"]
        if not any(item.get("id") == release_id for item in releases):
            releases.append(release)

        for track in resolved["tracks"]:
            track_id = track["id"]
            tracks[track_id] = track
            entry = track_sources.setdefault(
                track_id,
                {"release_ids": [], "track_ids": []},
            )
            if release_id not in entry["release_ids"]:
                entry["release_ids"].append(release_id)

    return {
        "tracks": list(tracks.values()),
        "track_sources": track_sources,
        "releases": releases,
        "cache": cache,
    }
