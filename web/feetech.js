"use strict";
/*
 * Leader arm on the laptop: reads the six Feetech STS3215 servos (IDs 1..6) over
 * Web Serial (Chrome / Edge, https page). READ ONLY: it never writes to the servos,
 * so the leader stays limp and free to move by hand.
 *
 * Packet: FF FF id len instr params.. checksum, checksum = ~(id+len+instr+params) & FF.
 * Present_Position is register 56, 2 bytes, little endian, bit 15 = sign.
 */
const FEETECH_VENDORS = [0x1a86];   // CH343 / CH340 USB chips on the SO-101 servo boards
const LEADER_IDS = [1, 2, 3, 4, 5, 6];
const ADDR_POSITION = 56;

function ftPacket(id, instr, params) {
  const len = params.length + 2;
  let sum = id + len + instr;
  for (const p of params) sum += p;
  return new Uint8Array([0xff, 0xff, id, len, instr, ...params, (~sum) & 0xff]);
}

/* Pull complete status packets out of a byte array. Returns {packets, used}. */
function ftParse(buf) {
  const packets = [];
  let i = 0;
  while (i + 5 < buf.length) {
    if (buf[i] !== 0xff || buf[i + 1] !== 0xff || buf[i + 2] === 0xff) { i++; continue; }
    const id = buf[i + 2], len = buf[i + 3];
    if (len < 2 || len > 64) { i++; continue; }
    const end = i + 4 + len;              // index after the checksum
    if (end > buf.length) break;          // wait for more bytes
    let sum = 0;
    for (let k = i + 2; k < end - 1; k++) sum += buf[k];
    if (((~sum) & 0xff) === buf[end - 1]) {
      packets.push({ id, err: buf[i + 4], data: buf.slice(i + 5, end - 1), bytes: buf.slice(i, end) });
      i = end;
    } else {
      i++;
    }
  }
  return { packets, used: i };
}

function ftPosition(lo, hi) {
  const v = lo | (hi << 8);
  const p = v & 0x8000 ? -(v & 0x7fff) : v;
  return Math.max(0, Math.min(4095, p));
}

class LeaderArm {
  constructor(port) {
    this.port = port;
    this.buf = [];
    this.waiters = [];
    this.useSync = true;
  }

  static supported() { return typeof navigator !== "undefined" && "serial" in navigator; }

  async open() {
    await this.port.open({ baudRate: 1000000 });
    this.writer = this.port.writable.getWriter();
    this.reader = this.port.readable.getReader();
    this.loop = (async () => {
      try {
        for (;;) {
          const { value, done } = await this.reader.read();
          if (done) break;
          for (const b of value) this.buf.push(b);
          this.waiters.splice(0).forEach((w) => w());
        }
      } catch (e) { this.error = e; }
    })();
  }

  async close() {
    try { await this.reader.cancel(); } catch (e) { /* already closed */ }
    try { this.reader.releaseLock(); } catch (e) { /* ignore */ }
    try { this.writer.releaseLock(); } catch (e) { /* ignore */ }
    try { await this.port.close(); } catch (e) { /* unplugged */ }
  }

  /* Send one packet and collect status packets until `want` ids answered or the time is up. */
  async ask(pkt, want, ms) {
    this.buf.length = 0;
    await this.writer.write(pkt);
    const got = new Map();
    const t0 = performance.now();
    const sent = Array.from(pkt).join(",");
    for (;;) {
      const { packets, used } = ftParse(this.buf);
      this.buf.splice(0, used);
      for (const p of packets) {
        if (Array.from(p.bytes).join(",") === sent) continue;   // some adapters echo what we send
        if (want.includes(p.id) && p.data.length >= 2) got.set(p.id, ftPosition(p.data[0], p.data[1]));
      }
      if (want.every((id) => got.has(id))) return got;
      const left = ms - (performance.now() - t0);
      if (left <= 0) return got;
      await new Promise((r) => { this.waiters.push(r); setTimeout(r, Math.min(left, 5)); });
    }
  }

  /* Positions of servos 1..6 (raw 0..4095), or null if not all answered. */
  async readPositions() {
    if (this.useSync) {
      const got = await this.ask(ftPacket(0xfe, 0x82, [ADDR_POSITION, 2, ...LEADER_IDS]), LEADER_IDS, 40);
      if (got.size === LEADER_IDS.length) return LEADER_IDS.map((id) => got.get(id));
      if (got.size === 0) this.useSync = false;   // board without sync read: ask one by one
    }
    const out = [];
    for (const id of LEADER_IDS) {
      const got = await this.ask(ftPacket(id, 0x02, [ADDR_POSITION, 2]), [id], 25);
      if (!got.has(id)) return null;
      out.push(got.get(id));
    }
    return out;
  }
}
