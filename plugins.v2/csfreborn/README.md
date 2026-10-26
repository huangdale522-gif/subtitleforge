# SubtitleForge MoviePilot Adapter

This V3 plugin listens for `TransferComplete` events and calls a running
SubtitleForge service. It intentionally does not embed a website scraper in the
MoviePilot process.

Configuration:

- `service_url`: the URL reachable from the MoviePilot container.
- `api_token`: the same token as `SF_API_TOKEN`.
- `languages`: `zh-Hans` and/or `zh-Hant`.
- `overwrite`: re-download even when a matching external subtitle exists.
- `notify`: send a MoviePilot notification after a successful download.

Both containers must see the media library at the same path. For example, if
MoviePilot sees a file as `/media/movies/Film.mkv`, SubtitleForge must also see
`/media/movies/Film.mkv`.

