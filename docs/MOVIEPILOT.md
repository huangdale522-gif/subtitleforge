# MoviePilot integration

The included adapter is `moviepilot_plugin/plugins.v3/csfreborn`.

It listens for `TransferComplete`, extracts the final video path and media
metadata, then calls:

```text
POST {service_url}/v1/process
```

## Installation options

For a custom plugin repository, publish `moviepilot_plugin` to a GitHub
repository while preserving this layout:

```text
plugins.v3/csfreborn/__init__.py
package.v3.json
```

Then add the repository's `main` branch URL to MoviePilot's `PLUGIN_MARKET`.

For a manual test, copy the plugin directory into MoviePilot's plugin source
directory, restart MoviePilot, and inspect the MoviePilot log for plugin loading
errors. The exact source directory depends on the MoviePilot version and
installation method.

## Configuration

In the MoviePilot plugin page:

1. Enable the plugin.
2. Leave `service_url` as `http://127.0.0.1:8600` when the plugin manages the
   bundled service.
3. Set `media_roots` to the media paths visible inside the MoviePilot
   container, commonly `/plex,/media`.
4. Paste the same token used by `SF_API_TOKEN` if the external service uses one.
5. Choose `zh-Hans` and/or `zh-Hant`.
6. Keep overwrite off unless you explicitly want replacement.
7. Configure `scan_paths` for foreign movies, foreign TV, documentaries, and
   anime, and keep Chinese-language libraries in `scan_exclude_paths`.
8. Keep `exclude_domestic_anime` enabled if domestic animation should not
   consume subtitle quota. The default regions are `CN,TW,HK`; path keywords
   such as `国漫` and `国创` are used only as a fallback.
9. Set `scan_max_files` for one batch and `scan_daily_download_limit` for the
   total successful downloads allowed per local day.
10. Leave `scan_dry_run` enabled for the first full-library inventory. The
   preview checks external and embedded subtitles without calling providers.

Use the plugin's `/health` endpoint to check the service.

## Library scan and notifications

Each scan inspects the complete configured library before querying any subtitle
provider. Files without the requested Chinese subtitle are stored in a
persistent queue; only queued items then consume provider downloads. Scheduled
and manual scans share the same daily ledger, and simultaneous triggers are
serialized.

External subtitles are associated with a video only when its name is followed
by a valid language or subtitle-purpose suffix. For example,
`Movie.2024.zh-cn.srt` does not count as a subtitle for `Movie.mkv`. Embedded
generic `zh` tracks also use their track titles to distinguish Simplified from
Traditional Chinese when that metadata is available.

Domestic-animation exclusion uses NFO `genre`, `origin_country`/`country`, and
`original_language` fields during scans, and MoviePilot `genre_ids`,
`origin_country`, and `production_countries` during `TransferComplete` events.
A Chinese title alone is not used as a classification signal, so Japanese
animation with a Chinese title remains eligible.

The plugin status page uses `GET /v1/status` to show the live local daily
ledger, queue due/deferred counts, and provider cooldown state. The configured
daily limit applies to all providers together. Quotas enforced by ASSRT,
SubtitleBest, OpenSubtitles, or a custom source remain authoritative on those
services and may change without notice.

The queue section groups movies separately and combines TV/anime episodes by
season. It reads artwork from MoviePilot/TMDB metadata, NFO poster fields, or
local `poster`/`folder`/`seasonXX-poster` files. Each queued video exposes a
manual subtitle picker backed by:

- `GET /api/v1/plugin/CSFReborn/queue`
- `POST /api/v1/plugin/CSFReborn/manual/search`
- `POST /api/v1/plugin/CSFReborn/manual/download`

The browser only receives public candidate metadata and short-lived selection
IDs. Provider URLs, tokens, and download references stay on the service.

When `notify` is enabled, MoviePilot sends the scan summary through its
configured Telegram or other notification channel. A successful scan message
lists the actual subtitle filenames written beside the videos, including
language and provider. Transfer-complete notifications use the same actual
filename format.

## Failure behavior

Provider failures are returned in the job result and displayed in plugin
history. A failed subtitle search does not roll back the media transfer and
does not crash MoviePilot.
