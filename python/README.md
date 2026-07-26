# Ratio Ghost — Python port

A minimal, headless Python port of [Ratio Ghost](http://ratioghost.com), the
BitTorrent tracker proxy that inflates your reported upload to improve your
share ratio. This port keeps **only the core proxy** from the original Tcl/Tk
application — there is no GUI, no auto-updater, no tray icon, and no other
extras. It is a straight port of the ratio algorithm in `rghost.vfs/lib/app-ghost/proxy.tcl`.

## How it works

Point your torrent client's tracker / HTTP proxy at this program. When the
client sends a tracker **announce** (a request carrying `info_hash`,
`downloaded`, `uploaded` and `left`), Ratio Ghost rewrites the `uploaded`
value using randomized ratios plus an occasional "boost" and forwards the
request to the real tracker. It reads the tracker's peer counts back so it
only inflates when there are enough leechers to look plausible, and it skips
any report that would look like cheating.

- **Plain HTTP** announces are intercepted and modified.
- **`CONNECT`** tunnels are passed through unmodified (as in the original).
- **Raw TLS** hitting the plain port is handed to the HTTPS port, terminated
  with the bundled `tls/server.crt` / `tls/server.key`, then processed like a
  plain request.

## Run locally

```bash
python3 python/ratioghost.py
```

Listens on `3773` (HTTP) and `3774` (HTTPS interception). Requires Python 3.8+
and the standard library only.

## Run with Docker

```bash
docker build -f python/Dockerfile -t ratioghost .
docker run --rm -p 3773:3773 -p 3774:3774 ratioghost
```

The image ships in **actual-upload mode**: `RG_NO_INFLATE=1` (report the real
upload, no inflation or boost) and `RG_NO_DOWNLOAD=1` (report download as 0).
Override either with `-e` to change behaviour.

### Docker Compose

A `docker-compose.yml` is provided at the repo root:

```bash
docker compose up -d --build
docker compose logs -f
docker compose down
```

Edit the `environment:` block in `docker-compose.yml` to change settings.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `RG_BIND` | `0.0.0.0` | Interface to bind |
| `RG_LISTEN_PORT` | `3773` | HTTP proxy port (HTTPS port is this + 1) |
| `RG_ONLY_LOCAL` | `0` | Only accept connections from `127.0.0.1` |
| `RG_ONLY_TRACKER` | `1` | Block anything that isn't tracker traffic |
| `RG_NO_INFLATE` | `0` | Report the real upload only — no ratio inflation, no boost |
| `RG_MIN_PEERS` | `5` | Minimum leechers before inflating upload (ignored when `RG_NO_INFLATE=1`) |
| `RG_UPUP_RATIO_A` / `RG_UPUP_RATIO_B` | `4.0` / `8.0` | Upload-vs-upload ratio range |
| `RG_UPDOWN_RATIO_A` / `RG_UPDOWN_RATIO_B` | `0.00` / `0.05` | Upload-vs-download ratio range |
| `RG_BOOST` | `15` | Boost magnitude (KB/s over elapsed time) |
| `RG_BOOST_CHANCE` | `5` | Percent chance of applying a boost |
| `RG_NO_DOWNLOAD` | `0` | Report download as 0 (and suppress `completed`) |
| `RG_SEED` | `0` | With `RG_NO_DOWNLOAD`, always report `left=0` (seeding) |
| `RG_AUTO_SEED` | `0` | With `RG_NO_DOWNLOAD`, leech until the client actually completes, then dynamically switch to seeding (`left=0`) |
| `RG_TLS_CERT` / `RG_TLS_KEY` | `tls/server.crt` / `tls/server.key` | HTTPS interception cert/key |
| `RG_LOGLEVEL` | `INFO` | Logging level |

Licensed under the GNU GPL v3, same as the original.
