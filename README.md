# Emby Theme Worker

Private, headless Python 3.12 worker that discovers missing Emby movie and series theme songs, produces a normalized `theme.mp3`, and verifies registration through Emby. It runs as an independent Docker container with no Emby plugin, web UI, or published port.

## Features

- Enumerates `Movie` and `Series` items through the Emby API with pagination and `HasThemeSong=false`.
- Restricts all work to a configured media root and checks both Emby `ThemeSongs` and the filesystem before writing.
- Uses exact provider identifiers first, then scores concurrent fuzzy candidates on title, year/type, theme intent, duration, and cross-source consensus.
- Never extracts a movie studio logo as a theme; series extraction requires Emby `IntroStart`/`IntroEnd` markers.
- Selects the highest-energy 45-second window, normalizes loudness, adds short fades, and emits 192 kbps MP3.
- Commits with a same-directory lock and atomic rename; existing themes are never overwritten.
- Stores resumable state, backoff, provider circuits, candidate evidence, and output hashes in SQLite.
- Exposes a CLI-only control plane for diagnostics, preview, bounded runs, status, scheduling, and Docker health checks.

## Safety model

- Only paths contained by `allowed_path` are eligible. The production container mounts `/Media` but not `/Adult`.
- The worker runs as `1000:1001` with a read-only root filesystem, all Linux capabilities dropped, and `no-new-privileges` enabled.
- STRM files must contain exactly one absolute HTTP(S) URL.
- URLs, query strings, API keys, cookies, and authorization values are redacted from logs.
- SQLite stores hashes of source URLs, never source URLs or credentials.
- The dedicated Emby API key and optional provider cookies are read from files below `/run/secrets`.
- A generated file is complete only after the Emby `ThemeSongs` endpoint returns it.

## Provider pipeline

Exact resolvers are collected in priority order and transport failures continue to the next candidate:

| Media type | Exact order |
| --- | --- |
| Anime | AnimeThemes strict title/year OP1 -> ThemerrDB TMDb -> Plex TVDb theme -> TelevisionTunes |
| Series | ThemerrDB TMDb -> Plex TVDb theme -> TelevisionTunes |
| Movie | ThemerrDB TMDb |

If the exact layer misses, the worker queries Archive.org, YouTube, Bilibili, NetEase, and QQ Music concurrently. Archive.org candidates require explicit Creative Commons or public-domain metadata. Anonymous Tunefind scraping is deliberately disabled; its official API adapter remains gated on issued credentials and documentation.

Resolver health and transport health are separate. A valid ThemerrDB mapping whose YouTube download fails opens the YouTube transport circuit, not the ThemerrDB resolver circuit.

Fuzzy candidates share a 100-point scorer with a default automatic threshold of 75. Covers, remixes, live performances, karaoke, trailers, reactions, and playlists receive penalties. The final fallback extracts the opening from the movie or earliest normal episode.

## Requirements

- Docker Engine with Compose v2
- An Emby server reachable from the container
- A dedicated Emby API key
- A writable media mount using the same paths Emby reports through its API
- Optional Netscape-format cookie files for providers that require authentication

The supplied image pins Python 3.12, FFmpeg 7.1.5, yt-dlp 2026.8.19, and all Python dependencies.

## Quick start

1. Clone and build:

   ```bash
   gh repo clone ump45nose/emby-theme-worker
   cd emby-theme-worker
   docker build --pull=false -t local/emby-theme-worker:0.2.0 .
   ```

2. Create persistent directories and copy the public configuration:

   ```bash
   install -d -m 0750 \
     /vol2/1000/Docker/emby-theme-worker/data \
     /vol2/1000/Docker/emby-theme-worker/config \
     /vol2/1000/Docker/emby-theme-worker/secrets
   install -m 0644 config/config.yaml \
     /vol2/1000/Docker/emby-theme-worker/config/config.yaml
   ```

3. Install a dedicated Emby API key without placing it in the repository:

   ```bash
   install -m 0400 /path/to/emby_api_key \
     /vol2/1000/Docker/emby-theme-worker/secrets/emby_api_key
   chown -R 1000:1001 /vol2/1000/Docker/emby-theme-worker
   ```

4. Review the host paths and identity in `compose.yaml`, then start the service:

   ```bash
   docker compose config --quiet
   docker compose up -d
   docker compose exec -T emby-theme-worker emby-theme-worker doctor
   docker compose exec -T emby-theme-worker emby-theme-worker preview --json
   ```

The supplied Compose file targets this private instance:

- Emby: `http://127.0.0.1:28096`
- Media: `/vol1/1000/Media:/Media:rw`
- Stack: `/vol2/1000/Docker/stacks/emby-theme-worker`
- Persistent state: `/vol2/1000/Docker/emby-theme-worker`
- Network mode: `host`

## CLI

```bash
# Connectivity, permissions, binaries, credentials, and SQLite
docker compose exec -T emby-theme-worker emby-theme-worker doctor

# Read-only scope and provider routing preview
docker compose exec -T emby-theme-worker emby-theme-worker preview --json
docker compose exec -T emby-theme-worker emby-theme-worker preview --json --item 136511

# Read-only provider resolution and scoring
docker compose exec -T emby-theme-worker emby-theme-worker probe --item 136511 --json

# Bounded manual work
docker compose exec -T emby-theme-worker emby-theme-worker run --item 136511
docker compose exec -T emby-theme-worker emby-theme-worker run --limit 10

# Audit and safely replace worker-owned legacy local openings
docker compose exec -T emby-theme-worker emby-theme-worker migrate-local --dry-run --type Movie --limit 25
docker compose exec -T emby-theme-worker emby-theme-worker migrate-local --dry-run --item EMBY_ID
docker compose exec -T emby-theme-worker emby-theme-worker migrate-local --type Movie --limit 1

# Emby intro-marker pilot for a single library
docker compose exec -T emby-theme-worker emby-theme-worker intro status --library 136475 --json
docker compose exec -T emby-theme-worker emby-theme-worker intro enable --library 136475
docker compose exec -T emby-theme-worker emby-theme-worker intro run --library 136475

# Progress and health
docker compose exec -T emby-theme-worker emby-theme-worker status --json
docker compose exec -T emby-theme-worker emby-theme-worker health
```

The default container command is `serve`. It evaluates `0 3 * * *` in `Asia/Taipei`. The first scheduled traversal is unlimited and resumable; later runs stop at 100 attempts or 25 successful registrations by default.

## Configuration

Public settings live in `config/config.yaml`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `emby_url` | `http://127.0.0.1:28096` | Emby endpoint reachable through host networking |
| `allowed_path` | `/Media` | Only eligible physical media root |
| `database_path` | `/data/worker.db` | SQLite state database |
| `schedule` | `30 9 * * *` | Cron schedule interpreted in `timezone`, after the fixed Emby downtime |
| `target_seconds` | `45` | Output duration |
| `providers.threshold` | `80` | Minimum fuzzy auto-selection score |
| `providers.replacement_threshold` | `85` | Minimum score for replacing a worker-owned local opening |
| `providers.ytdlp_search_timeout_seconds` | `60` | Hard wall-clock deadline for one yt-dlp search |
| `providers.ytdlp_download_timeout_seconds` | `180` | Hard wall-clock deadline for one yt-dlp download |
| `limits.network_concurrency` | `2` | Concurrent provider lookups |
| `limits.media_concurrency` | `1` | Download and FFmpeg concurrency boundary |
| `refresh.full_scan_after_scheduled_run` | `true` | Register pending files with one end-of-batch scan |

Cookie configuration contains file paths only. Missing optional cookie files disable cookie use without exposing secrets.

## Theme registration and state

Emby 4.9 does not register a newly written theme during per-item metadata refresh on this deployment. A generated file therefore remains `pending_refresh` until the end of a scheduled batch. The worker then requests one standard Emby media-library scan and verifies every pending item through `ThemeSongs`.

Manual `run --item` never starts the global scan. This keeps smoke tests bounded and distinguishes these states:

- `complete`: Emby returns the theme through `ThemeSongs`.
- `pending_refresh`: a worker-owned, hash-matched file awaits registration.
- `skipped_existing`: Emby already indexes a theme.
- `skipped_existing_unindexed`: an unrelated disk theme exists and is preserved.
- `failed`: provider, score, download, media, or refresh failure with retry metadata.

Backoff defaults are 30 days for no result, 7 days for low score, and 6 hours increasing through 24 hours, 3 days, and 7 days for transient network failures.

## Development and testing

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest -q
python -m compileall -q emby_theme_worker
```

The bounded smoke suite covers configuration validation, path containment, log redaction, scoring, STRM validation, atomic no-overwrite behavior, and existing-theme preservation.

## Backup and rollback

Keep `/data/worker.db` with the media files it audits. It records generated paths and hashes for later review.

To roll back, stop or recreate only this independent container with a retained image tag. Do not delete `/data`, and do not automatically remove generated `theme.mp3` files:

```bash
docker compose stop emby-theme-worker
docker tag local/emby-theme-worker:rollback-last-deployed local/emby-theme-worker:0.2.0
docker compose up -d --force-recreate
```

## Private-use notice

ThemerrDB entries can point to YouTube, and several fuzzy providers rely on scraping or unofficial download behavior. This worker is intended for a private media instance. Provider policies, regional restrictions, and anti-bot changes are expected operational failures handled by retries and provider circuits. Operators remain responsible for authorization and licensing of downloaded media.

## Acknowledgements

- [Themearr](https://github.com/Themearr/themearr) inspired candidate scoring and resumable provider-state design; this repository is not a fork.
- [ThemerrDB](https://github.com/LizardByte/ThemerrDB) supplies exact TMDb mappings.
- [AnimeThemes](https://animethemes.moe/) supplies anime opening metadata and media.
- [emby-theme-maker](https://github.com/Oratorian/emby-theme-maker) documents the full-library-scan registration behavior.

No GPL or AGPL plugin source is copied into this project.

## License

Licensed under the MIT License. See [LICENSE](LICENSE).
