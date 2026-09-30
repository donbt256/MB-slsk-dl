# MB-slsk-dl

Music acquisition automation built around MusicBrainz metadata, Soulseek/slskd discovery, deterministic matching, GitHub Actions, and a rolling set of GitHub music-library repositories.

The pipeline is designed to acquire complete releases, publish them atomically when possible, checkpoint progress after each release, and enrich the already-published files with OneTagger metadata.

## Features

- MusicBrainz-based metadata resolution
  - No Spotify credentials or Spotify API dependency.
  - Accepts album and track requests from `input.yaml`.
  - Resolves releases, release groups, recordings, artists, dates, ISRCs, track numbers, and artwork metadata.
  - Handles MusicBrainz edition/release-title differences through release-group fallbacks.
  - Caches resolved MusicBrainz data in `state/tracks.json`.
  - Throttles MusicBrainz requests and retries transient API failures.
- Soulseek acquisition through slskd
  - Runs `slskd/slskd:latest` inside the GitHub Actions runner.
  - Searches by album first for multi-track releases.
  - Falls back to individual track searches when an album match is incomplete or rejected.
  - Uses album+year retries, edition-label fallbacks, punctuation-normalized searches, and title-only searches where appropriate.
  - Album+year searches are retried three times; normal logical searches are not repeatedly hammered.
  - Waits for both the slskd HTTP API and the Soulseek connection before searching.
- Deterministic candidate matching
  - No LLM or OpenAI dependency.
  - Filters out non-audio files and files over GitHub's per-blob size limit.
  - Compares title, artist, album/path, track number, and alternate-version terminology.
  - Understands common filename formats such as numbered tracks and `Artist - Title`.
  - Penalizes or rejects unexpected live, acoustic, demo, remix, instrumental, karaoke, edit, radio-edit, piano, rehearsal, session, cover, tribute, and bootleg variants.
  - Album matching requires complete track coverage and consistent candidate files.
  - Prefers an accepted release over a merely higher-scoring release that fails the deterministic acceptance rules.
  - Persists matching evidence and matcher version in acquisition state.
- Durable state and resumability
  - `state/tracks.json` records MusicBrainz metadata, sources, search history, matching results, acquisition status, library location, and enrichment state.
  - State writes are atomic.
  - Runner-local downloads are never treated as durable merely because a previous run recorded them.
  - Previously published tracks are skipped.
  - A previously matched complete release can be recovered if a later transient Soulseek search returns no candidates.
  - Search and matching state is compacted so the state file does not grow without bound.
- Release-level processing and checkpoints
  - Releases are processed independently and sequentially.
  - Each release must complete search, download, and publication before it is checkpointed.
  - Successful releases create a checkpoint commit on `main`.
  - Checkpoint pushes retry after rebasing if the remote changed.
  - A failed release does not discard earlier successful release checkpoints.
  - If a release partially published before a later failure, its published state is preserved so already-published tracks are not uploaded again.
- Atomic publishing
  - Multi-track albums are treated as atomic publishing units: the album is not considered complete until all requested tracks are available.
  - One-track releases are handled individually.
  - The publisher stages changes in a local Git checkout before committing.
  - Library paths are derived from the resolved metadata and sanitized for Git paths.
  - GitHub's individual-file limit is respected with a 100 MiB safety ceiling.
- Rolling GitHub music library
  - Publishes into repositories named `music-library-001`, `music-library-002`, etc. by default.
  - Normal repositories target approximately 900 MiB rather than approaching GitHub's 1 GiB repository-size boundary.
  - Albums at or below 1 GiB are kept together rather than split across repositories.
  - Larger releases can be split only when required by the repository-size constraints.
  - Existing library state is inspected before publishing so new releases can continue from the current library.
- Post-publish metadata enrichment
  - Installs the headless OneTagger CLI in the workflow.
  - Runs OneTagger after the music itself has already been committed and pushed.
  - Configures MusicBrainz, Bandcamp, Deezer, iTunes, and Musixmatch metadata sources.
  - Writes normal tags including title, artist, album, album artist, genre, dates, catalog/track/release IDs, version, duration, track/disc numbering, label, explicit status, BPM, URL, ISRC, and other available metadata.
  - Embeds album artwork when available.
  - Retrieves synced and unsynced lyrics when available.
  - Writes optional `.lrc` lyric files.
  - Metadata changes are committed separately from the music acquisition commit.
  - The metadata stage is non-fatal: if OneTagger fails, the already-published music remains intact.
  - OneTagger completion state is persisted so completed enrichment is not needlessly repeated.
- Safe album removal
  - Put an exact album title in `remove.txt`.
  - Removals run before Soulseek acquisition.
  - Matching is case-insensitive by album title, with artist information used as an additional safeguard when available.
  - Removes the album's tracked library files, including metadata sidecars such as `.lrc`.
  - Removes the album's tracks from acquisition state.
  - Commits library deletion separately from acquisition.
- GitHub Actions operation
  - Runs on `workflow_dispatch`.
  - Uses Python 3.12.
  - Uses a concurrency group so two acquisition runs do not intentionally operate on the same pipeline simultaneously.
  - Prints release-by-release progress and a final acquisition-state diagnostic summary.
  - Always attempts to stop the slskd container.

## Configuration

Runtime settings are centralized in `config.yml`. The workflow loads this file before acquisition and exports the relevant values to the pipeline. Python stages also read it directly, so the same settings apply when individual scripts are run manually.

Retry/attempt counts are configured per operation rather than through one global retry value. These are maximum total attempts, including the initial attempt.

| Setting | Purpose |
| --- | --- |
| `library.repo_prefix` | Prefix for generated music-library repositories |
| `library.start_number` | First repository number to consider |
| `library.number_width` | Zero-padding for repository numbers |
| `library.soft_rollover_bytes` | Normal repository target before rolling over |
| `library.hard_rollover_bytes` | Maximum size for keeping a release atomic |
| `library.max_file_bytes` | Maximum individual file size accepted for the library |
| `library.artwork_reserve_bytes` | Space reserved when estimating release size |
| `github.git_data_max_attempts` | Maximum attempts for transient GitHub Git-data writes |
| `github.file_upload_max_attempts` | Maximum attempts for GitHub file uploads |
| `github.checkpoint_push_max_attempts` | Maximum attempts to push source-repository checkpoints |
| `musicbrainz.request_max_attempts` | Maximum attempts for transient MusicBrainz API requests |
| `github.request_timeout_seconds` | GitHub API request timeout |
| `soulseek.*` | Search timeouts, result limits, search retries, album+year attempts, and download slots |
| `download.*` | Download directory, polling, timeout, slow-user, and failed-file thresholds |
| `matcher.max_library_file_bytes` | Matcher-side file-size guard |
| `metadata.*` | OneTagger executable/configuration paths |
| `paths.*` | Input, removal, and persistent state file paths |

Example library settings:

```yaml
library:
  repo_prefix: "my-music-"
  start_number: 1
  number_width: 3
  soft_rollover_bytes: 943718400 # 900 MiB
  hard_rollover_bytes: 1073741824 # 1 GiB
  max_file_bytes: 104857600 # 100 MiB

musicbrainz:
  request_max_attempts: 5

soulseek:
  search_max_attempts: 5
  album_year_attempts: 3

github:
  git_data_max_attempts: 5
  file_upload_max_attempts: 5
  checkpoint_push_max_attempts: 3
```

Secrets remain in GitHub Actions secrets and are not stored in `config.yml`.

## Input

Edit `input.yaml`.

The file has two optional top-level lists: `albums` and `tracks`.

### Album request

```yaml
albums:
  - artist: "Radiohead"
    album: "In Rainbows"
```

An album request resolves the release through MusicBrainz and acquires all tracks in that release.

### Individual track request

```yaml
tracks:
  - artist: "Radiohead"
    album: "In Rainbows"
    title: "Nude"
```

Track requests resolve the specified album through MusicBrainz and select the requested recording by title.

Album and track requests can be mixed in the same file.

The current input format is intentionally structured rather than parsing `Artist - Album` strings. This avoids ambiguity for self-titled releases and names containing hyphens.

## Removing an album

Add the exact album title to `remove.txt`:

```text
The Album Title
```

Removal happens before Soulseek searches. The request removes matching acquisition state and tracked files from the appropriate music-library repository, including generated sidecar files.

Remove the line from `remove.txt` if the album should be eligible for acquisition again on a later run.

## Required GitHub secrets

The workflow expects:

| Secret | Purpose |
| --- | --- |
| `SOULSEEK_USERNAME` | Soulseek account username used by slskd |
| `SOULSEEK_PASSWORD` | Soulseek account password used by slskd |
| `GIT_PAT` | Token used to publish to the `music-library-*` repositories |

No OpenAI or Spotify credential is required.

## Workflow

The main workflow is `.github/workflows/musicbrainz.yml`.

The stages are:

1. Check out the repository.
2. Install Python 3.12 dependencies.
3. Resolve `input.yaml` through MusicBrainz.
4. Process queued album removals.
5. Start slskd and wait for its API and Soulseek connection.
6. Process each release independently:
   1. Search Soulseek.
   2. Deterministically match candidates.
   3. Download the selected files.
   4. Verify the complete release is available locally.
   5. Publish it to the rolling music-library repositories.
   6. Verify every track is marked published.
   7. Commit and push a release checkpoint.
7. Install the OneTagger CLI.
8. Run post-publish metadata enrichment.
9. Print the final acquisition-state summary.
10. Stop slskd.

Because the music is committed before OneTagger runs, a metadata failure cannot erase a successful music acquisition.

## Search and matching behavior

For an album, the normal search is based on the primary album artist and album title.

If the search produces no candidates, the pipeline can progressively broaden discovery:

- punctuation-normalized artist/album query;
- album title plus MusicBrainz-derived release year, with three attempts;
- album title with parenthesized edition labels removed;
- individual track searches for releases that still cannot be accepted as complete albums.

For individual tracks, a title-only fallback is available when the artist+title search returns nothing.

Discovery is deliberately broader than acceptance. A candidate can be found without being accepted. The deterministic matcher then evaluates whether it actually represents the requested track or release.

## Library layout and size limits

The publisher uses rolling repositories with the default prefix:

```text
music-library-001
music-library-002
music-library-003
...
```

The normal repository target is approximately 900 MiB.

The publisher also observes:

- 1 GiB atomic-release limit: albums at or below this size are kept together.
- 100 MiB individual-file ceiling: files above this size are not considered publishable candidates.
- GitHub's repository and Git blob constraints are therefore handled before a release is committed.

The library repository and branch are recorded in acquisition state for every published track.

## State

The primary persistent state file is:

```text
state/tracks.json
```

It contains, among other fields:

- MusicBrainz-derived track metadata;
- release and recording identifiers;
- source release IDs;
- acquisition status;
- selected Soulseek candidates;
- deterministic matching results;
- search IDs and query history;
- local download information;
- destination library repository and path;
- artwork/lyrics enrichment state.

The state is updated throughout the pipeline and is checkpointed after successful releases.

Typical acquisition statuses include:

- `pending`
- `matched`
- `unmatched`
- `downloaded`
- `ready_to_publish`
- `published`

## Post-publish OneTagger metadata

OneTagger is deliberately run after acquisition commits.

The configuration lives in:

```text
onetagger/autotagger.json
```

The configured metadata platforms are:

- MusicBrainz
- Bandcamp
- Deezer
- iTunes
- Musixmatch

The configuration enables album tagging, embedded artwork, standard metadata fields, synced/unsynced lyrics, and `.lrc` output.

Metadata is committed independently from the music. This separation means:

- successful music publication is durable before enrichment starts;
- a OneTagger failure does not roll back or invalidate music;
- enrichment can be retried independently.

## Failure and recovery model

The workflow is designed around durable release checkpoints rather than one giant all-or-nothing run.

If a release fails during search or download, its runner-local files are not treated as durable and the release is retried on the next workflow run.

If a release has already published files before a later stage fails, the published state is retained and future runs skip those tracks.

If a Git push for a checkpoint is rejected because the remote advanced, the checkpoint logic fetches, rebases, and retries the push.

If Soulseek temporarily returns no results for a release that already has a complete durable match in state, the previous complete match can be reused instead of immediately abandoning the release.

## Running it

1. Update `input.yaml`.
2. Optionally add exact album titles to `remove.txt`.
3. Ensure the three required GitHub Actions secrets are configured.
4. Open the repository's Actions tab.
5. Run `MusicBrainz Music Acquisition` with `workflow_dispatch`.
6. Monitor the release-level log output and checkpoint commits.

The workflow is intended to be run repeatedly. Existing published state is preserved between runs, so a later run can continue from the last successful checkpoints rather than starting the entire library over.

## Repository structure

```text
.github/workflows/musicbrainz.yml   GitHub Actions workflow
input.yaml                           Album/track acquisition requests
remove.txt                           Queued album removals
scripts/main.py                      MusicBrainz resolution and state creation
scripts/musicbrainz.py               MusicBrainz client and metadata resolution
scripts/search.py                    Soulseek search and matching orchestration
scripts/matcher.py                   Deterministic candidate/release matcher
scripts/download.py                  Soulseek download handling
scripts/process_releases.py          Release-level orchestration/checkpointing
scripts/publish.py                   Rolling GitHub library publisher
scripts/remove_albums.py             Safe album removal
scripts/metadata.py                  Post-publish OneTagger integration
scripts/release.py                   Release grouping and release keys
onetagger/autotagger.json            OneTagger configuration
state/tracks.json                    Persistent acquisition state
```

## Dependencies

The acquisition scripts use Python 3.12 and the packages listed in `requirements.txt`.

The workflow additionally uses:

- GitHub Actions;
- Docker;
- slskd;
- Soulseek;
- OneTagger CLI;
- MusicBrainz;
- MusicBrainz Cover Art Archive;
- Git/GitHub for library publication and checkpoints.

## Design principles

The project intentionally separates:

1. metadata identity — MusicBrainz defines what was requested;
2. discovery — Soulseek finds possible files;
3. acceptance — deterministic matching decides whether a candidate is suitable;
4. acquisition — slskd downloads the selected files;
5. publication — Git commits make completed music durable;
6. enrichment — OneTagger adds metadata after the music is already safe.

This separation prevents a failed discovery, download, or metadata-enrichment step from silently turning into a false claim that an album was successfully published.
