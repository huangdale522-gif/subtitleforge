# NAS deployment

## Required information

Before installing on a NAS, confirm:

- NAS operating system and CPU architecture.
- Docker or Container Manager version.
- MoviePilot container name and Compose project/network.
- Host media path and its path inside the MoviePilot container.
- Whether MoviePilot is V2 or V3. The included adapter targets V3.

## Synology example

Assume:

```text
Host media path:       /volume1/media
MoviePilot media path: /media
MoviePilot container:  moviepilot
```

Place the project at `/volume1/docker/subtitleforge`, set:

```dotenv
SF_MEDIA_ROOTS=/media
MEDIA_ROOT=/volume1/media
```

Then:

```bash
cd /volume1/docker/subtitleforge
sh deploy/install.sh
```

If MoviePilot and SubtitleForge are on the same Docker network, set the plugin
service URL to `http://subtitleforge:8600`. Otherwise use
`http://NAS_LAN_IP:8600`.

## Permissions

The container runs as UID/GID `1000` by default. The mounted media directory must
allow that user to create subtitle files. If your media files are owned by a
different UID, create the container with the matching user or grant write access
to the media library. Do not run the service as root merely to avoid fixing
permissions.

## Upgrade

```bash
cd /volume1/docker/subtitleforge
docker compose pull
docker compose up -d --build
```

Job history is stored in `./data/jobs.sqlite3`.

## Security

- Keep port 8600 on the LAN or behind a reverse proxy with authentication.
- Set a long `SF_API_TOKEN`.
- Do not expose provider credentials to MoviePilot; only the service needs them.
- Keep `SF_MEDIA_ROOTS` as narrow as possible. The API rejects paths outside it.

