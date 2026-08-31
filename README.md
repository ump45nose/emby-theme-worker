# Emby Theme Worker

Private, headless Python 3.12 worker that writes one normalized `theme.mp3` per Emby movie or series. It is intentionally independent from Emby plugins and has no web UI or published port.

## Safety boundary

- Emby is read through its API and refreshed only after a successful atomic file commit.
- Only paths contained by `/Media` are eligible. `/Adult` is not mounted into the container.
- Existing themes are never overwritten. The worker rechecks Emby and disk state immediately before committing.
- STRM files must contain exactly one absolute HTTP(S) URL. URLs, query strings, API keys, cookies, and authorization values are redacted from logs.
- SQLite stores hashes of source URLs, not the source URLs or credentials.
- Cookie files and the dedicated Emby API key are read from `/run/secrets` only.

## Provider order

- Anime: AnimeThemes exact title/year OP1, ThemerrDB TMDb, Plex TVDb theme.
- Series: ThemerrDB TMDb, Plex TVDb theme.
- Movies: ThemerrDB TMDb.
- Fuzzy fallback: Archive.org (CC/public-domain metadata required), TelevisionTunes, Tunefind metadata, YouTube, Bilibili, NetEase metadata, and QQ Music metadata.
- Final fallback: first 45 seconds after initial silence from the movie or earliest normal episode, including STRM.

Fuzzy candidates share a 100-point scorer and default automatic threshold of 75. Online sources use their highest-energy continuous 45-second segment. All output is normalized to 192 kbps MP3 with loudness normalization and short fades, then validated by ffprobe.

## Commands

```text
emby-theme-worker doctor
emby-theme-worker preview --json
emby-theme-worker run --item 136511
emby-theme-worker run --limit 10
emby-theme-worker status --json
emby-theme-worker health
emby-theme-worker serve
```

The default service command is `serve`. It evaluates `0 3 * * *` in `Asia/Taipei`. The first scheduled traversal is unlimited and resumable through SQLite; later traversals stop at 100 attempts or 25 successes.

Emby 4.9 does not register newly written theme files during a per-item metadata refresh. Generated files therefore remain `pending_refresh` until the end of a scheduled batch, when the worker requests one standard Emby media-library scan and then verifies every pending item through `ThemeSongs`. Manual `run --item` never starts that global scan; it is safe for bounded generation smoke tests and reports the pending state honestly.

## Deployment

The production stack lives at `/vol2/1000/Docker/stacks/emby-theme-worker`, while mutable state and secrets live below `/vol2/1000/Docker/emby-theme-worker`. The service uses host networking and reaches Emby at `http://127.0.0.1:28096`.

```bash
docker build -t local/emby-theme-worker:0.1.0 .
docker compose config
docker compose up -d
docker compose exec emby-theme-worker emby-theme-worker doctor
```

## Private-use notice

ThemerrDB entries can point to YouTube, and several fuzzy providers rely on scraping or unofficial download behavior. This worker is for this private instance only. Provider policy or anti-bot changes are expected failures and are isolated with retries and provider circuits.
