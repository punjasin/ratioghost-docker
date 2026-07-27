// Ratio Ghost — torrent seeder announcer (Bun)
//
// Scans a folder for .torrent files and periodically announces each one to its
// tracker(s) as a *complete seeder* (left=0, downloaded=0, uploaded=0). It does
// NOT actually connect to peers or serve any data — it only reports the seeding
// status to the tracker. The folder is re-scanned every cycle so newly dropped
// torrents are picked up automatically.
//
// Config (environment):
//   RG_TORRENTS_DIR   folder to scan          (default: ./torrents)
//   RG_INTERVAL_MIN   minutes between cycles  (default: 30)
//   RG_PORT           port reported to tracker(default: 6881)
//   RG_TIMEOUT_SEC    per-tracker timeout     (default: 15)
//
// Run:  bun run bun/seeder.ts

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { createHash, randomBytes } from "node:crypto";
import dgram from "node:dgram";

const TORRENTS_DIR = process.env.RG_TORRENTS_DIR ?? "./torrents";
const INTERVAL_MIN = Number(process.env.RG_INTERVAL_MIN ?? 30);
const PORT = Number(process.env.RG_PORT ?? 6881);
const TIMEOUT_MS = Number(process.env.RG_TIMEOUT_SEC ?? 15) * 1000;

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------
function log(msg: string) {
  const t = new Date().toTimeString().slice(0, 8);
  console.log(`${t} ${msg}`);
}

// ---------------------------------------------------------------------------
// Minimal bencode decoder. Strings are returned as Buffers (binary-safe). While
// decoding the top-level dict we record the byte range of the `info` value so
// the info-hash can be taken from the original bytes exactly.
// ---------------------------------------------------------------------------
type Bencode = number | Buffer | Bencode[] | { [k: string]: Bencode };

function decodeTorrent(buf: Buffer): { meta: Record<string, Bencode>; infoStart: number; infoEnd: number } {
  let pos = 0;
  let depth = 0;
  let infoStart = -1;
  let infoEnd = -1;

  function readString(): Buffer {
    let s = pos;
    while (buf[pos] !== 0x3a /* : */) pos++;
    const len = parseInt(buf.toString("ascii", s, pos), 10);
    pos++; // skip ':'
    const out = buf.subarray(pos, pos + len);
    pos += len;
    return out;
  }

  function decodeItem(): Bencode {
    const c = buf[pos];
    if (c === 0x69 /* i */) {
      pos++;
      let s = pos;
      while (buf[pos] !== 0x65 /* e */) pos++;
      const n = Number(buf.toString("ascii", s, pos));
      pos++;
      return n;
    }
    if (c === 0x6c /* l */) {
      pos++;
      const arr: Bencode[] = [];
      while (buf[pos] !== 0x65) arr.push(decodeItem());
      pos++;
      return arr;
    }
    if (c === 0x64 /* d */) {
      pos++;
      depth++;
      const obj: Record<string, Bencode> = {};
      while (buf[pos] !== 0x65) {
        const key = readString().toString("utf8");
        if (key === "info" && depth === 1) infoStart = pos;
        const val = decodeItem();
        if (key === "info" && depth === 1) infoEnd = pos;
        obj[key] = val;
      }
      pos++;
      depth--;
      return obj;
    }
    if (c >= 0x30 && c <= 0x39) return readString();
    throw new Error(`bad bencode byte 0x${c?.toString(16)} at ${pos}`);
  }

  const meta = decodeItem() as Record<string, Bencode>;
  return { meta, infoStart, infoEnd };
}

function bstr(v: Bencode | undefined): string {
  if (v === undefined) return "";
  return Buffer.isBuffer(v) ? v.toString("utf8") : String(v);
}

function trackersOf(meta: Record<string, Bencode>): string[] {
  const out = new Set<string>();
  if (meta.announce) out.add(bstr(meta.announce));
  const list = meta["announce-list"];
  if (Array.isArray(list)) {
    for (const tier of list) {
      if (Array.isArray(tier)) for (const u of tier) out.add(bstr(u));
    }
  }
  return [...out].filter(Boolean);
}

// ---------------------------------------------------------------------------
// Percent-encode raw bytes for a tracker query string (RFC 3986 unreserved).
// ---------------------------------------------------------------------------
function pct(bytes: Buffer): string {
  let s = "";
  for (const b of bytes) {
    const unreserved =
      (b >= 0x30 && b <= 0x39) || // 0-9
      (b >= 0x41 && b <= 0x5a) || // A-Z
      (b >= 0x61 && b <= 0x7a) || // a-z
      b === 0x2d || b === 0x2e || b === 0x5f || b === 0x7e; // - . _ ~
    s += unreserved ? String.fromCharCode(b) : "%" + b.toString(16).padStart(2, "0").toUpperCase();
  }
  return s;
}

// ---------------------------------------------------------------------------
// HTTP(S) tracker announce (BEP 3).
// ---------------------------------------------------------------------------
async function announceHttp(
  tracker: string,
  infoHash: Buffer,
  peerId: Buffer,
  keyHex: string,
  eventStr: string,
): Promise<string> {
  const sep = tracker.includes("?") ? "&" : "?";
  const q = [
    "info_hash=" + pct(infoHash),
    "peer_id=" + pct(peerId),
    "port=" + PORT,
    "uploaded=0",
    "downloaded=0",
    "left=0",
    "compact=1",
    "numwant=0",
    "key=" + keyHex,
  ];
  if (eventStr) q.push("event=" + eventStr);

  const res = await fetch(tracker + sep + q.join("&"), {
    headers: { "User-Agent": "qBittorrent/5.0.0" },
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  const buf = Buffer.from(await res.arrayBuffer());

  try {
    const { meta } = decodeTorrent(buf);
    const fail = meta["failure reason"];
    if (fail !== undefined) return `fail: ${bstr(fail)}`;
    const seeders = meta.complete ?? "?";
    const leechers = meta.incomplete ?? "?";
    const interval = meta.interval ?? "?";
    return `ok (seeders ${seeders}/${leechers}, interval ${interval}s)`;
  } catch {
    return `ok (HTTP ${res.status})`;
  }
}

// ---------------------------------------------------------------------------
// UDP tracker announce (BEP 15): connect, then announce.
// ---------------------------------------------------------------------------
const EVENT_CODE: Record<string, number> = { none: 0, completed: 1, started: 2, stopped: 3 };

function announceUdp(
  host: string,
  port: number,
  infoHash: Buffer,
  peerId: Buffer,
  keyNum: number,
  eventStr: string,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = dgram.createSocket("udp4");
    const txConnect = randomBytes(4);
    let txAnnounce = txConnect;
    let stage: "connect" | "announce" = "connect";

    const timer = setTimeout(() => finish(new Error("timeout")), TIMEOUT_MS);
    function finish(err: Error | null, ok?: string) {
      clearTimeout(timer);
      try { socket.close(); } catch {}
      if (err) reject(err); else resolve(ok!);
    }

    socket.on("error", (e) => finish(e));
    socket.on("message", (msg) => {
      try {
        if (stage === "connect") {
          if (msg.length < 16 || msg.readUInt32BE(0) !== 0 || !msg.subarray(4, 8).equals(txConnect)) return;
          const connId = msg.subarray(8, 16);
          stage = "announce";
          txAnnounce = randomBytes(4);
          const req = Buffer.alloc(98);
          connId.copy(req, 0);
          req.writeUInt32BE(1, 8); // action: announce
          txAnnounce.copy(req, 12);
          infoHash.copy(req, 16);
          peerId.copy(req, 36);
          req.writeBigUInt64BE(0n, 56); // downloaded
          req.writeBigUInt64BE(0n, 64); // left
          req.writeBigUInt64BE(0n, 72); // uploaded
          req.writeUInt32BE(EVENT_CODE[eventStr] ?? 0, 80);
          req.writeUInt32BE(0, 84); // ip
          req.writeUInt32BE(keyNum >>> 0, 88); // key
          req.writeInt32BE(0, 92); // num_want (0 = we don't need peers)
          req.writeUInt16BE(PORT, 96);
          socket.send(req, port, host, (e) => { if (e) finish(e); });
        } else {
          if (msg.length < 8 || !msg.subarray(4, 8).equals(txAnnounce)) return;
          const action = msg.readUInt32BE(0);
          if (action === 3) return finish(null, `fail: ${msg.subarray(8).toString("utf8")}`);
          if (action !== 1 || msg.length < 20) return finish(new Error("bad announce reply"));
          const interval = msg.readUInt32BE(8);
          const leechers = msg.readUInt32BE(12);
          const seeders = msg.readUInt32BE(16);
          finish(null, `ok (seeders ${seeders}/${leechers}, interval ${interval}s)`);
        }
      } catch (e) {
        finish(e as Error);
      }
    });

    const req = Buffer.alloc(16);
    req.writeBigUInt64BE(0x41727101980n, 0); // magic protocol id
    req.writeUInt32BE(0, 8); // action: connect
    txConnect.copy(req, 12);
    socket.send(req, port, host, (e) => { if (e) finish(e); });
  });
}

// ---------------------------------------------------------------------------
// Per-torrent identity, kept stable across cycles so the tracker sees one peer.
// ---------------------------------------------------------------------------
const peerIds = new Map<string, Buffer>();
const keys = new Map<string, string>();
const started = new Set<string>();

function peerIdFor(hashHex: string): Buffer {
  let id = peerIds.get(hashHex);
  if (!id) {
    // qBittorrent 5.0.0 style prefix + random.
    id = Buffer.concat([Buffer.from("-qB5000-"), randomBytes(12)]).subarray(0, 20);
    peerIds.set(hashHex, id);
  }
  return id;
}
function keyFor(hashHex: string): string {
  let k = keys.get(hashHex);
  if (!k) { k = randomBytes(4).toString("hex"); keys.set(hashHex, k); }
  return k;
}

async function announceTracker(tracker: string, infoHash: Buffer, hashHex: string, eventStr: string) {
  const peerId = peerIdFor(hashHex);
  try {
    let result: string;
    if (tracker.startsWith("udp://")) {
      const u = new URL(tracker);
      const port = Number(u.port || 0);
      if (!port) { log(`  ! ${tracker} — no port, skipped`); return; }
      const keyNum = parseInt(keyFor(hashHex), 16);
      result = await announceUdp(u.hostname, port, infoHash, peerId, keyNum, eventStr);
    } else if (tracker.startsWith("http://") || tracker.startsWith("https://")) {
      result = await announceHttp(tracker, infoHash, peerId, keyFor(hashHex), eventStr);
    } else {
      log(`  ! ${tracker} — unsupported scheme, skipped`);
      return;
    }
    log(`  → ${tracker} ${result}`);
  } catch (e) {
    log(`  ✗ ${tracker} — ${(e as Error).message}`);
  }
}

// ---------------------------------------------------------------------------
// One scan + announce cycle.
// ---------------------------------------------------------------------------
async function cycle() {
  let files: string[];
  try {
    files = readdirSync(TORRENTS_DIR).filter((f) => f.toLowerCase().endsWith(".torrent"));
  } catch (e) {
    log(`Cannot read ${TORRENTS_DIR}: ${(e as Error).message}`);
    return;
  }

  if (files.length === 0) {
    log(`No .torrent files in ${TORRENTS_DIR}`);
    return;
  }

  log(`Scanning ${files.length} torrent(s) in ${TORRENTS_DIR}`);
  for (const file of files) {
    let infoHash: Buffer;
    let trackers: string[];
    let name: string;
    try {
      const buf = readFileSync(join(TORRENTS_DIR, file));
      const { meta, infoStart, infoEnd } = decodeTorrent(buf);
      if (infoStart < 0) throw new Error("no info dict");
      infoHash = createHash("sha1").update(buf.subarray(infoStart, infoEnd)).digest();
      trackers = trackersOf(meta);
      const info = meta.info as Record<string, Bencode> | undefined;
      name = bstr(info?.name) || file;
    } catch (e) {
      log(`✗ ${file} — parse error: ${(e as Error).message}`);
      continue;
    }

    const hashHex = infoHash.toString("hex");
    const eventStr = started.has(hashHex) ? "none" : "started";
    started.add(hashHex);

    log(`${name} [${hashHex.slice(0, 12)}…] ${trackers.length} tracker(s) — seeding (0/0)`);
    if (trackers.length === 0) { log("  ! no trackers in file"); continue; }
    await Promise.allSettled(trackers.map((t) => announceTracker(t, infoHash, hashHex, eventStr)));
  }
}

// ---------------------------------------------------------------------------
// Main loop.
// ---------------------------------------------------------------------------
log(`Ratio Ghost seeder — dir=${TORRENTS_DIR} interval=${INTERVAL_MIN}m port=${PORT}`);
await cycle();
setInterval(cycle, INTERVAL_MIN * 60 * 1000);
