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
- `scan_dry_run`: only inspect/search and never write subtitles. This is enabled
  by default until library identification has been reviewed.
- `scan_interval_hours`: scheduled interval in hours.
- `scan_max_files`: maximum videos checked per run; the cursor resumes with the
  least recently checked files on the next run.
- `scan_provider_order`: optional provider order used only by scanning.
- `scan_video_extensions`: comma-separated video file extensions.
- Provider credentials for SubtitleBest, ASSRT, OpenSubtitles, and an optional
  custom JSON source. Blank credential fields preserve values already stored in
  the service's private `.env` file.

The scan recognizes external subtitles by filename/content and embedded
subtitles through `ffprobe`. Existing Chinese subtitles are skipped unless
`overwrite` is enabled. An authenticated `POST /api/v1/plugin/CSFReborn/scan`
starts a manual scan using the saved settings.

Both containers must see the media library at the same path. For example, if
MoviePilot sees a file as `/media/movies/Film.mkv`, SubtitleForge must also see
`/media/movies/Film.mkv`.
