**Ratio Ghost**

Ratio Ghost is a simple program to allow you to increase your BitTorrent ratio.

You can find usage information and compiled binaries for Windows, Linux, and Mac at http://ratioghost.com

![Ratio Ghost Screenshot](http://ratioghost.com/public/img/screen1.png)

This repository is a Docker-oriented fork. Alongside the original Tcl/Tk
application it contains two headless components:

| Path | What it is |
|---|---|
| [`python/`](python/README.md) | Headless Python port of the tracker proxy — the core of the original, no GUI/updater/tray. Listens on `3773` (HTTP) and `3774` (HTTPS interception). |
| [`bun/`](bun/README.md) | Bun script that scans a folder of `.torrent` files and periodically announces each one to its tracker(s) as a complete seeder. |
| `rghost.vfs/` | The original Tcl/Tk application, unchanged. |

Each component has its own README with full configuration details.


**Running the proxy with Docker Compose**

`docker-compose.yml` at the repo root builds and runs the Python proxy:

```
docker compose up -d --build
docker compose logs -f
docker compose down
```

It ships in *actual-upload mode* — `RG_NO_INFLATE=1` (report the real upload,
no inflation or boost), `RG_NO_DOWNLOAD=1` (report download as 0) and
`RG_AUTO_SEED=1` (leech until the client actually completes, then switch to
reporting `left=0`). Edit the `environment:` block to change any of this; see
[`python/README.md`](python/README.md) for the full variable list.

Then point your torrent client's HTTP proxy at the host on port `3773`.


**Running the seeder**

The seeder is not part of the Compose file; build and run it separately:

```
mkdir -p torrents            # drop your .torrent files here
docker build -f bun/Dockerfile -t ratioghost-seeder .
docker run --rm -v "$PWD/torrents:/app/torrents" ratioghost-seeder
```

The folder is re-scanned every cycle, so new `.torrent` files are picked up
automatically. See [`bun/README.md`](bun/README.md).


**Running From Source**


Ratio Ghost requires [Tcl/Tk](http://tcl.tk/) version 8.6.


With Tcl/Tk 8.6 installed, simply open a terminal in this directory and run:

```
wish rghost.vfs/main.tcl
```

The headless ports can be run directly too:

```
python3 python/ratioghost.py     # proxy (Python 3.8+, stdlib only)
bun run bun/seeder.ts            # seeder
```
