#!/usr/bin/env python3
#   Ratio Ghost (Python port) - BitTorrent ratio modifying proxy
#   Original Copyright (C) 2006-2015 Yasmine@RatioGhost.com
#   Python port keeps only the core proxy: no GUI, no auto-update, no tray.
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.

"""Ratio Ghost - a BitTorrent tracker proxy.

It listens on an HTTP proxy port. Point your torrent client's tracker/HTTP
proxy at it. Tracker announce requests (the ones carrying ``info_hash``,
``downloaded``, ``uploaded`` and ``left``) are intercepted and the reported
``uploaded`` value is inflated using randomized ratios plus an occasional
"boost", then forwarded to the real tracker. Everything else is passed
through (or blocked, depending on settings).

This is a faithful port of the algorithm in the original Tcl ``proxy.tcl``.
"""

import asyncio
import logging
import os
import random
import re
import ssl
import time

log = logging.getLogger("ratioghost")


# ---------------------------------------------------------------------------
# Settings (environment driven, same defaults as the original settings.dat)
# ---------------------------------------------------------------------------
def _envf(name, default):
    return float(os.environ.get(name, default))


def _envi(name, default):
    return int(os.environ.get(name, default))


def _envb(name, default):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self):
        self.bind = os.environ.get("RG_BIND", "0.0.0.0")
        self.listen_port = _envi("RG_LISTEN_PORT", 3773)
        # The original always uses listen_port + 1 for the HTTPS side.
        self.listen_port_https = self.listen_port + 1

        # Only accept connections from 127.0.0.1. Off by default here so the
        # container is reachable from the host; the original defaults it on.
        self.only_local = _envb("RG_ONLY_LOCAL", 0)
        # Block anything that is not a tracker announce/scrape.
        self.only_tracker = _envb("RG_ONLY_TRACKER", 1)

        # Report the real upload only: no ratio inflation, no boost.
        self.no_inflate = _envb("RG_NO_INFLATE", 0)

        self.min_peers = _envi("RG_MIN_PEERS", 5)
        self.upup_ratio_a = _envf("RG_UPUP_RATIO_A", 4.0)
        self.upup_ratio_b = _envf("RG_UPUP_RATIO_B", 8.0)
        self.updown_ratio_a = _envf("RG_UPDOWN_RATIO_A", 0.00)
        self.updown_ratio_b = _envf("RG_UPDOWN_RATIO_B", 0.05)

        self.boost = _envf("RG_BOOST", 15)
        self.boost_chance = _envf("RG_BOOST_CHANCE", 5)

        self.no_download = _envb("RG_NO_DOWNLOAD", 0)
        self.seed = _envb("RG_SEED", 0)

        self.tls_cert = os.environ.get("RG_TLS_CERT", "tls/server.crt")
        self.tls_key = os.environ.get("RG_TLS_KEY", "tls/server.key")


settings = Settings()


# ---------------------------------------------------------------------------
# Formatting helpers (ports of FormatData / FormatElapsed from util.tcl)
# ---------------------------------------------------------------------------
def format_data(num):
    num = float(num)
    if num == 0:
        return "0"
    for n, p in ((1099511627776, "TB"), (1073741824, "GB"), (1048576, "MB"), (1024, "KB")):
        if num > n:
            return f"{num / n:0.1f}{p}"
    return f"{round(num)}B"


def format_elapsed(num):
    num = float(num)
    if num == 0:
        return "0"
    for n, p in ((86400, "d"), (3600, "h"), (60, "m")):
        if num > n:
            return f"{num / n:0.1f}{p}"
    return f"{round(num)}s"


def event(msg):
    """Mirror of the Tcl Event proc: a human readable activity line."""
    log.info(msg)


# ---------------------------------------------------------------------------
# Per-torrent statistics (keyed by the raw, url-encoded info_hash string)
# ---------------------------------------------------------------------------
actual_first = {}       # hash -> (down, up, left) first values we ever saw
actual_last = {}        # hash -> (down, up, left) last actual values
actual_sum = {}         # hash -> [down, up] accumulated actual transfer
reported_last = {}      # hash -> (down, up, left) last values we reported
reported_sum = {}       # hash -> [down, up] accumulated reported transfer
reported_last_time = {}  # hash -> unix time of last report
response = {}           # hash -> {complete, incomplete, interval} from tracker
hosts = {}              # "host:port" -> request count


def stats_actual(h, ev, down, up, left):
    """Track the client's real numbers, returning the deltas we need.

    Returns (prev_down, prev_up, down_diff, up_diff).
    """
    prev_down = prev_up = 0

    if h not in actual_first:
        actual_first[h] = (down, up, left)
    if h not in actual_sum:
        actual_sum[h] = [0, 0]

    if h in actual_last and ev != "started":
        d, u, _l = actual_last[h]
        actual_sum[h][0] += (down - d)
        actual_sum[h][1] += (up - u)
        prev_down = d
        prev_up = u

    down_diff = down - prev_down
    up_diff = up - prev_up
    actual_last[h] = (down, up, left)
    return prev_down, prev_up, down_diff, up_diff


def stats_reported(h, ev, down, up, left):
    if h not in reported_sum:
        reported_sum[h] = [0, 0]
    if h in reported_last and ev != "started":
        d, u, _l = reported_last[h]
        reported_sum[h][0] += (down - d)
        reported_sum[h][1] += (up - u)
    reported_last_time[h] = int(time.time())
    reported_last[h] = (down, up, left)


# ---------------------------------------------------------------------------
# The ratio faking algorithm (port of the info_hash branch in read_first)
# ---------------------------------------------------------------------------
def _qval(query, name):
    m = re.search(name + r"=([^&]+)", query)
    return m.group(1) if m else ""


def fake_announce(query, host, port):
    """Given the request query string, return the (possibly) modified query.

    Returns the query string to forward, or ``None`` to signal the request
    should be dropped (either blocked, or skipped to avoid detection).
    """
    info_hash = _qval(query, "info_hash")
    ev = _qval(query, "event")
    downloaded = _qval(query, "downloaded")
    uploaded = _qval(query, "uploaded")
    left = _qval(query, "left")

    key = "%s:%s" % (host, port)
    hosts[key] = hosts.get(key, 0) + 1

    h = info_hash

    if not (downloaded and uploaded and left):
        # Has an info_hash but no transfer numbers: this is a scrape or
        # similar. Nothing to fake, just forward it untouched.
        event(f"{host}:{port} Non-announce traffic.")
        return query

    down = int(downloaded)
    up = int(uploaded)
    left_v = int(left)

    _pd, _pu, down_diff, up_diff = stats_actual(h, ev, down, up, left_v)

    reported_prev_up = 0
    elapsed = 0
    if ev != "started":
        if h in reported_last:
            _rd, reported_prev_up, _rl = reported_last[h]
        if h in reported_last_time:
            elapsed = int(time.time()) - reported_last_time[h]

    post = f"{host}:{port} down/up from {format_data(down)}/{format_data(up)} to "

    if settings.no_download:
        _fd, _fu, fl = actual_first[h]
        down = 0
        left_v = fl
        if settings.seed:
            left_v = 0
        if ev == "completed":
            # Strip the completed event so the tracker never records it.
            if re.search(r"&event=completed", query):
                query = re.sub(r"&event=completed", "", query, count=1)
            else:
                query = re.sub(r"event=completed&", "", query, count=1)

    last_peers = response.get(h, {}).get("incomplete", 0)

    if not settings.no_inflate and last_peers >= settings.min_peers:
        down_ratio = settings.updown_ratio_b + random.random() * (
            settings.updown_ratio_a - settings.updown_ratio_b)
        up_ratio = settings.upup_ratio_b + random.random() * (
            settings.upup_ratio_a - settings.upup_ratio_b)

        new_up = reported_prev_up + up_diff
        new_up += down_ratio * down_diff
        new_up += up_ratio * up_diff

        if random.random() * 100 < settings.boost_chance:
            boost_val = settings.boost * 1024 * elapsed * random.random()
            new_up += boost_val
    else:
        # Not enough leechers to plausibly explain upload, only report real.
        new_up = reported_prev_up + up_diff

    if ev != "started" and new_up < reported_prev_up:
        # Reporting less than last time would look like cheating - drop it.
        event(f"({host}) LOGIC ERROR - SKIPPING TO AVOID DETECTION")
        return None

    up = int(f"{new_up:.0f}")

    # Splice the modified downloaded / uploaded / left back into the query.
    query = re.sub(r"downloaded=([^&]+)", "downloaded=%d" % down, query)
    query = re.sub(r"uploaded=([^&]+)", "uploaded=%d" % up, query)
    query = re.sub(r"left=([^&]+)", "left=%d" % left_v, query)

    post += f"{format_data(down)}/{format_data(up)}"
    if ev:
        post += f" ({ev})"
    event(post)

    stats_reported(h, ev, down, up, left_v)
    return query, h  # h returned so the caller can attribute the response


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------
async def _read_n(reader, n):
    buf = b""
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


async def _read_headers(reader):
    headers = {}
    raw = []
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        raw.append(line)
        try:
            k, v = line.decode("latin1").split(":", 1)
            headers[k.strip().lower()] = v.strip()
        except ValueError:
            pass
    return headers, b"".join(raw)


def _close(writer):
    try:
        writer.close()
    except Exception:
        pass


async def _pump(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        _close(writer)


async def _raw_relay(client_reader, client_writer, host, port, prefix=b"",
                     ssl_ctx=None):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(
            host, port, ssl=ssl_ctx)
    except Exception as e:
        log.debug("Couldn't open socket to %s:%s: %s", host, port, e)
        _close(client_writer)
        return
    if prefix:
        remote_writer.write(prefix)
        await remote_writer.drain()
    await asyncio.gather(
        _pump(client_reader, remote_writer),
        _pump(remote_reader, client_writer),
    )


_CLIENT_SSL = ssl.create_default_context()
_CLIENT_SSL.check_hostname = False
_CLIENT_SSL.verify_mode = ssl.CERT_NONE
try:
    _CLIENT_SSL.set_ciphers("DEFAULT@SECLEVEL=0")
except ssl.SSLError:
    pass


async def _forward_get(client_reader, client_writer, target, req_headers,
                       tls_side):
    """Handle a GET request: fake the announce and relay to the origin."""
    m = re.match(r"(?i)(https?)://([-a-z0-9.]+):?([0-9]+)?(.*)", target)
    if m:
        scheme = m.group(1).lower()
        host = m.group(2)
        port = m.group(3)
        path = m.group(4) or "/"
    else:
        # Origin-form request (e.g. decrypted TLS): host comes from the header.
        host_hdr = req_headers.get("host", "")
        if not host_hdr:
            _close(client_writer)
            return
        scheme = "https" if tls_side else "http"
        if ":" in host_hdr:
            host, port = host_hdr.rsplit(":", 1)
        else:
            host, port = host_hdr, ""
        path = target if target.startswith("/") else "/" + target

    origin_tls = tls_side or scheme == "https"
    if not port:
        port = "443" if origin_tls else "80"
    port = int(port)

    query = path.split("?", 1)[1] if "?" in path else ""
    hash_key = None

    if "info_hash=" in query:
        result = fake_announce(query, host, port)
        if result is None:
            _close(client_writer)
            return
        if isinstance(result, tuple):
            new_query, hash_key = result
        else:
            new_query = result
        base = path.split("?", 1)[0]
        path = base + "?" + new_query
    else:
        if settings.only_tracker:
            event(f"{host}:{port} Blocked non-tracker traffic.")
            _close(client_writer)
            return
        event(f"{host}:{port} Forwarding non-tracker traffic.")

    # Rebuild the request in origin-form with a corrected Host header and
    # Connection: close (announces are one-shot; keeps the port model simple).
    host_header = host if port in (80, 443) else f"{host}:{port}"
    lines = [f"GET {path} HTTP/1.1", f"Host: {host_header}"]
    for k, v in req_headers.items():
        if k in ("host", "proxy-connection", "connection"):
            continue
        lines.append(f"{k}: {v}")
    lines.append("Connection: close")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin1")

    ssl_ctx = _CLIENT_SSL if origin_tls else None
    try:
        remote_reader, remote_writer = await asyncio.open_connection(
            host, port, ssl=ssl_ctx)
    except Exception as e:
        log.debug("Couldn't open socket to %s:%s: %s", host, port, e)
        _close(client_writer)
        return

    remote_writer.write(request)
    await remote_writer.drain()

    # Relay the tracker response back, scanning it for peer counts so the
    # next announce can decide whether min_peers is met.
    accum = b""
    try:
        while True:
            chunk = await remote_reader.read(65536)
            if not chunk:
                break
            client_writer.write(chunk)
            await client_writer.drain()
            if hash_key is not None and len(accum) < 65536:
                accum += chunk
    except Exception:
        pass
    finally:
        _close(remote_writer)
        _close(client_writer)

    if hash_key is not None and accum:
        _parse_peer_counts(hash_key, accum)


def _parse_peer_counts(h, data):
    counts = {}
    found = False
    for t in ("complete", "incomplete", "interval"):
        pat = ("%d:%s" % (len(t), t)).encode() + rb"i(\d+)e"
        m = re.search(pat, data)
        counts[t] = int(m.group(1)) if m else 0
        found = found or m is not None
    response[h] = counts
    if found:
        event("  peers %d/%d, interval %s" % (
            counts["complete"], counts["incomplete"],
            format_elapsed(counts["interval"])))


async def handle_client(reader, writer, tls_side=False):
    peer = writer.get_extra_info("peername")
    addr = peer[0] if peer else "?"

    if settings.only_local and not tls_side and addr != "127.0.0.1":
        event(f"Blocked request from {addr}.")
        _close(writer)
        return

    try:
        first3 = await _read_n(reader, 3)
        if not first3:
            _close(writer)
            return

        if first3 in (b"GET", b"CON"):
            rest = await reader.readline()
            line = (first3 + rest).decode("latin1").rstrip("\r\n")
            parts = line.split(" ")
            if len(parts) < 2:
                _close(writer)
                return
            verb, target = parts[0].upper(), parts[1]
            req_headers, _raw = await _read_headers(reader)

            if verb == "CONNECT":
                # Establish a blind tunnel (HTTPS via CONNECT is passed
                # through unmodified, matching the original behaviour).
                writer.write(b"HTTP/1.0 200 Connection Established\r\n"
                             b"Connection: close\r\n\r\n")
                await writer.drain()
                if ":" in target:
                    thost, tport = target.rsplit(":", 1)
                else:
                    thost, tport = target, "443"
                event(f"Tunnel to {thost}:{tport}")
                await _raw_relay(reader, writer, thost, int(tport))
                return

            await _forward_get(reader, writer, target, req_headers, tls_side)
            return

        # Not HTTP: looks like a raw TLS handshake landing on the plain port.
        if tls_side:
            # Already came through TLS termination; give up.
            _close(writer)
            return
        event("Intercepting https request.")
        await _raw_relay(reader, writer, "127.0.0.1",
                         settings.listen_port_https, prefix=first3)
    except Exception as e:
        log.debug("connection error: %s", e)
        _close(writer)


async def main():
    logging.basicConfig(
        level=os.environ.get("RG_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, tls_side=False),
        settings.bind, settings.listen_port)

    https_server = None
    if os.path.exists(settings.tls_cert) and os.path.exists(settings.tls_key):
        try:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            # The bundled cert is a legacy 1024-bit / SHA1 self-signed cert;
            # drop the OpenSSL security level so it still loads (the original
            # Tcl TLS did no such checks).
            ssl_ctx.set_ciphers("DEFAULT@SECLEVEL=0")
            ssl_ctx.load_cert_chain(settings.tls_cert, settings.tls_key)
            https_server = await asyncio.start_server(
                lambda r, w: handle_client(r, w, tls_side=True),
                settings.bind, settings.listen_port_https, ssl=ssl_ctx)
        except (ssl.SSLError, OSError) as e:
            # Never let a bad cert take down the HTTP proxy.
            log.warning("Could not enable HTTPS interception (%s); "
                        "continuing HTTP only", e)
            https_server = None

    if https_server:
        event(f"Listening on {settings.bind}:{settings.listen_port} & "
              f"{settings.bind}:{settings.listen_port_https} (https)")
    else:
        if not (os.path.exists(settings.tls_cert)
                and os.path.exists(settings.tls_key)):
            log.warning("TLS cert/key not found (%s / %s); HTTPS off",
                        settings.tls_cert, settings.tls_key)
        event(f"Listening on {settings.bind}:{settings.listen_port}")

    async with server:
        if https_server:
            async with https_server:
                await asyncio.gather(server.serve_forever(),
                                     https_server.serve_forever())
        else:
            await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
