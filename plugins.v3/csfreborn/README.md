# SubtitleForge MoviePilot Adapter

This plugin listens for `TransferComplete` events and can also scan the media
library on a schedule. It can start the bundled SubtitleForge service inside the
MoviePilot container and uses MoviePilot's own title, year and IMDb metadata.

Configuration:

- `service_url`: the URL reachable from the MoviePilot container.
- `media_roots`: comma-separated media paths that the service may read.
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
- `xunlei_enabled`: credential-free exact-file fingerprint source, enabled by
  default.
- `shooter_enabled`: optional upstream Shooter fingerprint source, disabled by
  default because its endpoint frequently times out.

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

The plugin page reads `GET /v1/status` and shows the live local daily ledger,
queue due/deferred counts, provider configuration, and any 24-hour rate-limit
cooldowns. `scan_daily_download_limit` is one global cap across all providers,
not a separate allowance for each source. ASSRT, SubtitleBest, OpenSubtitles,
and custom sources can also enforce account quotas which this plugin cannot
query in advance.

When the bundled service is managed by the plugin, it uses the paths visible in
the MoviePilot container. For the common MoviePilot layout, `media_roots` is
`/plex,/media` and the scan paths start with `/plex`.
