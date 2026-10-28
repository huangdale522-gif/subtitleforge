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
2. Set `service_url`.
3. Paste the same token used by `SF_API_TOKEN`.
4. Choose `zh-Hans` and/or `zh-Hant`.
5. Keep overwrite off unless you explicitly want replacement.
6. Configure `scan_paths` for foreign movies, foreign TV, documentaries, and
   anime, and keep Chinese-language libraries in `scan_exclude_paths`.
7. Set `scan_max_files` for one batch and `scan_daily_download_limit` for the
   total successful downloads allowed per local day.
8. Leave `scan_dry_run` enabled for the first full-library inventory. The
   preview checks external and embedded subtitles without calling providers.

Use the plugin's `/health` endpoint to check the service.

## Library scan and notifications

Each scan inspects the complete configured library before querying any subtitle
provider. Files without the requested Chinese subtitle are stored in a
persistent queue; only queued items then consume provider downloads. Scheduled
and manual scans share the same daily ledger, and simultaneous triggers are
serialized.

When `notify` is enabled, MoviePilot sends the scan summary through its
configured Telegram or other notification channel. A successful scan message
lists the actual subtitle filenames written beside the videos, including
language and provider. Transfer-complete notifications use the same actual
filename format.

## Failure behavior

Provider failures are returned in the job result and displayed in plugin
history. A failed subtitle search does not roll back the media transfer and
does not crash MoviePilot.

## Built-in providers (no service required)

Since 0.4.0 the plugin also embeds the ChineseSubFinder subtitle sources, so it
works without a running SubtitleForge service.

- Sources: SubtitleBest (IMDb, including season packages), ASSRT (title
  search), Xunlei (file `cid` fingerprint), Shooter (file hash fingerprint).
- Credentials are the existing `subtitle_best_api_key` / `assrt_token`
  fields — no extra configuration.
- Language is decided from subtitle content, not from site labels; GBK/BIG5
  files are converted to UTF-8.
- Optional: `ffsubsync` for timeline correction, `7z`/`unrar`/`bsdtar` for
  rar/7z archives (zip works out of the box).

`native_mode` selects the order:

| Value | Behavior |
| --- | --- |
| `auto` (default) | Native providers first; the service is called only when a requested language is still missing. |
| `native_only` | Never call the service. |
| `service_first` | Call the service first; fall back to native on failure. |
| `off` | Previous behavior: service only. |

Scanning follows the same order: in `auto` the built-in engine scans the
library and the service is only used when no built-in source is available or
the native scan fails. `native_only` always uses the built-in engine,
`service_first` calls the service first and falls back, and `off` keeps the
previous service-only behavior.

## Pending queue

Videos that still lack a requested language are stored in a persistent queue.
The plugin page renders two checkbox tables: movies (one row each) and TV
seasons (one row per season with the missing episode list). Selecting a season
retries all of its missing episodes.

Press "下载选中" to retry, or use `POST /queue/download` with `{"keys": [...]}`
(movie row id `m:<key>` or season row id `s:<hash>`) or `{"all": true}`.
`scan_dry_run` defaults to true, which is why a preview scan reports zero
downloads — it only queues.

## Diagnostics

`GET /diagnose` returns a secret-free self-check: active mode, provider
readiness, service reachability, queue size and the last five results with
their `errors` and `notes`. Candidate-level skips (wrong language, unparsable
archive) are reported as `notes`, not `errors`; a history entry with
`下载 1 · 错误 0 · 提示 1` means the subtitle was written and one candidate was
skipped.
