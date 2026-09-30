# MB-slsk-dl

MusicBrainz metadata -> Soulseek search/matching -> download -> rolling music library.

## Input

Edit `urls.txt`. Each line can be a MusicBrainz release URL, release-group URL, or an `Artist - Album` query.

The workflow does not require Spotify credentials.

MusicBrainz metadata is retrieved through the public MusicBrainz Web Service with the required application identification and request throttling. Album artwork comes from the MusicBrainz Cover Art Archive.

## Secrets

- `SOULSEEK_USERNAME`
- `SOULSEEK_PASSWORD`
- `GIT_PAT`

Run the GitHub Action manually with `workflow_dispatch`.

## Metadata enrichment

Each release is still committed and pushed to the music-library repository before metadata enrichment begins.

After all acquisition releases have been processed, the workflow runs the headless OneTagger CLI against the already-committed library files. It writes normal metadata, embedded album artwork, synced/unsynced lyrics when available, and optional .lrc lyrics files.

Metadata is a separate commit (Add OneTagger metadata). A OneTagger failure does not undo or invalidate the music commits. The metadata step is intentionally non-fatal to the acquisition workflow.

The configuration is in onetagger/autotagger.json. It uses MusicBrainz, Bandcamp, Deezer, iTunes, and Musixmatch. Musixmatch does not require a separate API credential in OneTagger's platform implementation.


## Removing an album

Put an exact album title on its own line in `remove.txt`. The workflow processes removals before Soulseek starts. It removes the album's tracked library files, including metadata sidecars such as `.lrc`, removes the album's tracks from acquisition state, and commits the deletion separately. Matching is case-insensitive and based on the album title; do not leave a removal request in the file if you later want that album acquired again.

