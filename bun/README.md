# Ratio Ghost — seeder announcer (Bun)

A small Bun script that scans a folder for `.torrent` files and periodically
**announces each one to its tracker(s) as a complete seeder** (`left=0`,
`downloaded=0`, `uploaded=0`).

It does **not** actually connect to peers or serve any data — it only reports
the seeding status to the tracker. The folder is **re-scanned every cycle**, so
new `.torrent` files dropped in are picked up automatically. Supports both
HTTP(S) trackers (BEP 3) and UDP trackers (BEP 15).

Each torrent keeps a stable `peer_id` and `key` for the lifetime of the process
and sends `event=started` on its first announce, then plain periodic announces.

## Run locally

```bash
mkdir -p torrents            # drop your .torrent files here
bun run bun/seeder.ts
```

## Run with Docker

```bash
docker build -f bun/Dockerfile -t ratioghost-seeder .
docker run --rm -v "$PWD/torrents:/app/torrents" ratioghost-seeder
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `RG_TORRENTS_DIR` | `./torrents` | Folder scanned for `.torrent` files |
| `RG_INTERVAL_MIN` | `30` | Minutes between scan + announce cycles |
| `RG_PORT` | `6881` | Port reported to the tracker |
| `RG_TIMEOUT_SEC` | `15` | Per-tracker request timeout |

## Notes

- Announcing as a seeder without serving data is a form of ratio faking; only
  use it where you are permitted to. Many trackers detect peers that never
  actually transfer.
- Sending a fixed 30-minute interval may be shorter than a tracker's own
  `interval`; if a tracker complains, raise `RG_INTERVAL_MIN`.
