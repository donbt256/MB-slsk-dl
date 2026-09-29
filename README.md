# MB-slsk-dl

MusicBrainz metadata -> Soulseek search/matching -> download -> rolling music library.

## Input

Edit `urls.txt`. Each line can be a MusicBrainz release URL, release-group URL, or an `Artist - Album` query.

The workflow does not require Spotify credentials.

MusicBrainz metadata is retrieved through the public MusicBrainz Web Service with the required application identification and request throttling. Album artwork comes from the MusicBrainz Cover Art Archive.

## Secrets

- `SOULSEEK_USERNAME`
- `SOULSEEK_PASSWORD`
- `OPENAI_API_KEY`
- `GIT_PAT`

Run the GitHub Action manually with `workflow_dispatch`.
