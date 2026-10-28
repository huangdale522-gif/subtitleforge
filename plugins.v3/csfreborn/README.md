# SubtitleForge MoviePilot Adapter

This plugin listens for `TransferComplete` events and can also scan the media
library on a schedule. It calls a running SubtitleForge service and does not
embed website scrapers in the MoviePilot process.

Configuration:

- `service_url`: the URL reachable from the MoviePilot container.
- `api_token`: the same token as `SF_API_TOKEN`.
- `languages`: `zh-Hans` and/or `zh-Hant`.
- `overwrite`: re-download even when a matching external subtitle exists.
- `notify`: send a MoviePilot notification after a successful download.
- `provider_order`: provider preference order.
- `scan_enabled`: enable the scheduled library scan.
- `scan_paths`: filesystem roots to scan. Defaults target foreign movies,
  foreign TV, documentaries, and anime on the NAS layout.
- `scan_exclude_paths`: paths excluded from scanning, for example Chinese films
  and domestic TV.
- `scan_dry_run`: only inspect and never write subtitles. This is enabled
  by default until library identification has been reviewed. Preview mode does
  not query subtitle providers, so it consumes no provider quota.
- `scan_interval_hours`: scheduled interval in hours.
- `scan_max_files`: maximum queued videos processed by one scan run.
- `scan_daily_download_limit`: maximum successful subtitle downloads allowed on
  one local calendar day. The ledger is shared by scheduled and manual scans
  and survives service restarts.
- `scan_provider_order`: optional provider order used only by scanning.
- `scan_languages`: languages required by scanning. The default is Simplified
  Chinese only to reduce provider download quota; set both languages explicitly
  if required.
- `scan_video_extensions`: comma-separated video file extensions.
- Provider credentials for SubtitleBest, ASSRT, OpenSubtitles, and an optional
  custom JSON source. Blank credential fields preserve values already stored in
  the service's private `.env` file.

The scan recognizes external subtitles by filename/content and embedded
subtitles through `ffprobe`. Every configured video is inspected before any
subtitle source is queried. Blu-ray `BDMV/STREAM` fragments are ignored because
they are not standalone media files. Existing Chinese subtitles are skipped
unless `overwrite` is enabled; missing items are saved in a persistent queue.
Providers are queried only while consuming that queue, in configured order, and
later providers are skipped when the first one returns usable candidates. Rate-limit
or quota responses put that provider into a 24-hour cooldown, and each video is
limited to three download attempts. Cooldowns persist across service restarts.
When `notify` is enabled, the notification lists the actual subtitle filenames
that were downloaded successfully through MoviePilot's configured Telegram or
other notification channel. An authenticated `POST
/api/v1/plugin/CSFReborn/scan` starts a manual scan using saved settings.

Both containers must see the media library at the same path. For example, if
MoviePilot sees a file as `/media/movies/Film.mkv`, SubtitleForge must also see
`/media/movies/Film.mkv`.

## Built-in providers (0.4.0+)

The plugin can now complete subtitles without a running SubtitleForge service.
SubtitleBest (IMDb, including season packages), ASSRT (title search), Xunlei
(file `cid` fingerprint) and Shooter (file hash fingerprint) run inside the
MoviePilot process and reuse the existing `subtitle_best_api_key` /
`assrt_token` fields — no new configuration is required.

`native_mode` controls the order:

- `auto` (default): built-in providers first for transfer events; the service is
  only called when a requested language is still missing afterwards. Scheduled
  scans use the service while it is healthy (keeping its queue and quota) and
  fall back to the built-in engine when it is not.
- `native_only`: never call the service.
- `service_first`: service first, built-in providers as fallback.
- `off`: previous behavior, service only.

Additional options: `native_xunlei`, `native_shooter`, `native_tag_style`
(`zh-CN/zh-TW`, `chs/cht`, `zh-Hans/zh-Hant`), `native_auto_sync` (requires
`ffsubsync`), `native_encoding` (GBK/BIG5 to UTF-8), `native_max_subs` and
`native_request_delay`.

Subtitles are written beside the video as `<video>.<lang>.<ext>`. Language is
verified from subtitle content, and Xunlei/Shooter results are checked against
the target episode and year before they are accepted.

Self-check: `python3 tests/v3/csfreborn/check_native_engine.py`.
