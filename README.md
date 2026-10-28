# SubtitleForge

SubtitleForge is an independently maintained, provider-driven Chinese subtitle
downloader for a NAS. It was designed after reviewing ChineseSubFinder's
architecture and the current MoviePilot V3 plugin contracts, but it does not
copy the stopped project's site-specific implementation.

The important change is architectural: subtitle sites are adapters. A broken
source can be disabled or replaced without changing the scanner, matcher,
installation logic, or MoviePilot integration.

## What is included

- FastAPI service with API-token authentication.
- SubtitleBest share-sub adapter (account key plus API client token).
- Official OpenSubtitles API adapter (API key plus username/password for downloads).
- ASSRT adapter (token).
- Xunlei adapter using the exact video file CID fingerprint.
- Optional Shooter adapter using the exact video file hash fingerprint.
- Custom HTTP manifest adapter for your own source.
- Media-id/title matching, season/episode filtering, machine-translation filtering.
- External and embedded subtitle detection using `ffprobe`.
- Safe ZIP extraction and atomic subtitle placement.
- SQLite job history.
- MoviePilot V3 adapter that listens for `TransferComplete`.
- Full-library scans with external and embedded Chinese subtitle detection,
  a persistent missing-subtitle queue, exclusions, and dry-run preview.
- Blu-ray `BDMV/STREAM` segments are ignored so disc fragments are not treated
  as independent movies.
- Separate per-run and per-day download limits, including a daily ledger that
  survives service restarts.
- Docker image and Compose deployment.

The default output names are:

```text
Movie.2024.1080p.mkv
Movie.2024.1080p.chi.zh-cn.srt
Movie.2024.1080p.zh-tw.ass
```

## Source policy

SubtitleBest, OpenSubtitles, and ASSRT require accounts and credentials. Xunlei
is credential-free and enabled by default because it matches the exact video
fingerprint. Shooter follows the protocol retained by the upstream
ChineseSubFinder project, but is disabled by default because its endpoint often
times out from NAS networks. Provider quotas and terms can change.
For another source, implement the JSON manifest contract below or add a provider
adapter under `src/subtitleforge/providers/`.

No subtitle source can be guaranteed to remain available. The health endpoint
shows which providers are configured, and provider errors are recorded per job.

## Local test

```bash
cd subtitleforge
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run pytest
uv run uvicorn subtitleforge.api:app --host 127.0.0.1 --port 8600
```

Open <http://127.0.0.1:8600/docs>. If `SF_API_TOKEN` is set, pass:

```text
Authorization: Bearer <token>
```

## Configuration

Copy `.env.example` to `.env`. The most important values are:

| Variable | Meaning |
| --- | --- |
| `SF_API_TOKEN` | Bearer token used by MoviePilot and API clients |
| `SF_MEDIA_ROOTS` | Comma-separated allowed media roots |
| `SF_SUBTITLE_BEST_API_KEY` | SubtitleBest account API key |
| `SF_SUBTITLE_BEST_HEADER_TOKEN` | SubtitleBest API client token |
| `SF_OPENSUBTITLES_API_KEY` | OpenSubtitles API key |
| `SF_OPENSUBTITLES_USERNAME` | OpenSubtitles account username |
| `SF_OPENSUBTITLES_PASSWORD` | OpenSubtitles account password |
| `SF_ASSRT_TOKEN` | ASSRT API token |
| `SF_XUNLEI_ENABLED` | Enable the Xunlei file-fingerprint source |
| `SF_SHOOTER_ENABLED` | Enable the optional Shooter source |
| `SF_CUSTOM_PROVIDER_URL` | Optional custom manifest URL |
| `SF_ALLOW_MACHINE_TRANSLATED` | Allow machine-translated candidates |

Provider order is configured with `SF_PROVIDER_ORDER`, for example:

```text
SF_PROVIDER_ORDER=xunlei,assrt,subtitle_best,opensubtitles,custom,shooter
```

## API

### Process a file synchronously

```bash
curl -X POST http://127.0.0.1:8600/v1/process \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "video_path": "/media/movies/Movie.2024.1080p.mkv",
    "title": "Movie",
    "year": 2024,
    "media_type": "movie",
    "languages": ["zh-Hans", "zh-Hant"]
  }'
```

### Search without downloading

```bash
curl -X POST http://127.0.0.1:8600/v1/search \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Movie",
    "year": 2024,
    "media_type": "movie",
    "languages": ["zh-Hans"]
  }'
```

### Queue a background job

Use `POST /v1/jobs` with the same body as `/v1/process`, then poll
`GET /v1/jobs/{job_id}`.

### Scan the media library

`POST /v1/scan` first walks every configured directory, parses movie/TV metadata,
and detects existing Chinese subtitles without contacting any provider. Missing
items are saved in a persistent SQLite queue. The scanner then consumes only a
bounded number of queued items:

```json
{
  "paths": ["/media/movies/foreign", "/media/tv/foreign"],
  "exclude_paths": ["/media/movies/chinese"],
  "languages": ["zh-Hans", "zh-Hant"],
  "max_files": 10,
  "daily_download_limit": 20,
  "dry_run": true
}
```

With `dry_run: true`, the scanner only reads local subtitles and `ffprobe`
metadata; it does not call subtitle providers, so a preview consumes no source
quota and does not modify the download queue. Real runs use provider failover in
configured order and stop querying after the first provider returns usable
candidates. A provider that reports a rate limit or quota error enters a
24-hour cooldown. HTTP 5xx, Cloudflare gateway errors, and connection failures
use a shorter 15-minute circuit break so a temporary outage does not prevent
failover for the rest of the queue. Each video is limited to three subtitle
download attempts, and cooldown state is persisted locally across service
restarts.

`max_files` limits one scan batch. `daily_download_limit` limits successful
subtitle downloads for the local calendar day across every scanner instance
using the same data directory. Both limits apply to each provider together.
Queue retries are deferred for `retry_after_hours` after a failed task, and
concurrent scan requests are serialized so they cannot overspend the daily
ledger. The MoviePilot adapter can send the actual downloaded subtitle filenames
through the user's configured Telegram/notification channel.

`GET /v1/status?daily_download_limit=10` returns the current local-day success
count, remaining quota, due/deferred queue counts, and sanitized provider
configuration/cooldown state. The MoviePilot plugin uses this endpoint to render
its live status dashboard. The daily cap is global across all providers; the
external services may enforce separate account quotas that are not knowable in
advance.

## Custom provider contract

The service sends these query parameters:

```text
q, original_title, english_title, year, imdb_id, tmdb_id,
media_type, season, episode, languages
```

The endpoint should return a JSON list or `{"items": [...]}`:

```json
{
  "items": [
    {
      "id": "unique-id",
      "title": "Movie",
      "release_name": "Movie.2024.1080p.WEB-DL.srt",
      "language": "zh-Hans",
      "format": "srt",
      "url": "https://your-source.example/subtitle.srt",
      "year": 2024,
      "season": null,
      "episode": null,
      "score": 8,
      "download_count": 42,
      "machine_translated": false
    }
  ]
}
```

The URL must return the subtitle file directly, or a ZIP containing supported
subtitle files.

## NAS deployment

1. Create a directory on the NAS, for example `/volume1/docker/subtitleforge`.
2. Copy this `subtitleforge` directory there.
3. Copy `.env.example` to `.env` and set credentials and `SF_MEDIA_ROOTS`.
4. Set `MEDIA_ROOT` to the host path that corresponds to `/media`.
5. Run `docker compose up -d --build`.
6. Verify `curl http://NAS_IP:8600/healthz`.

The container must mount the same media paths as MoviePilot. A path mismatch is
the most common reason a MoviePilot event finds a file that SubtitleForge cannot
open.

See [NAS deployment](docs/NAS_DEPLOY.md) and
[MoviePilot integration](docs/MOVIEPILOT.md).

## Licensing and attribution

SubtitleForge is MIT licensed. ChineseSubFinder is also MIT licensed; its
license and attribution are retained in this repository. The Xunlei and Shooter
fingerprint protocols were reimplemented in Python from the upstream project,
which stopped receiving source updates in 2023. SubtitleForge does not bundle
the Go application.
