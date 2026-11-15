"""Cititor minimal MaxMind DB (.mmdb) + actualizare automată DB-IP Country Lite (CC BY 4.0)."""
import gzip
import ipaddress
import os
import struct
import threading
import time
import urllib.request

from .util import log

MARKER = b"\xab\xcd\xefMaxMind.com"


class MMDB:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.buf = f.read()
        i = self.buf.rfind(MARKER)
        if i < 0:
            raise ValueError("nu e fișier mmdb")
        self.meta, _ = self._decode(i + len(MARKER), 0, absolute=True)
        self.node_count = self.meta["node_count"]
        self.rs = self.meta["record_size"]
        self.ip_version = self.meta.get("ip_version", 6)
        self.tree_size = (self.rs * 2 // 8) * self.node_count
        self.data_start = self.tree_size + 16
        self._v4start = None

    # --- arbore ---
    def _record(self, node, bit):
        rs = self.rs
        b = self.buf
        if rs == 24:
            o = node * 6 + bit * 3
            return (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]
        if rs == 28:
            o = node * 7
            if bit == 0:
                return ((b[o + 3] & 0xF0) << 20) | (b[o] << 16) | (b[o + 1] << 8) | b[o + 2]
            return ((b[o + 3] & 0x0F) << 24) | (b[o + 4] << 16) | (b[o + 5] << 8) | b[o + 6]
        if rs == 32:
            o = node * 8 + bit * 4
            return struct.unpack(">I", b[o:o + 4])[0]
        raise ValueError("record_size nesuportat")

    def _v4_start(self):
        if self._v4start is None:
            node = 0
            if self.ip_version == 6:
                for _ in range(96):
                    if node >= self.node_count:
                        break
                    node = self._record(node, 0)
            self._v4start = node
        return self._v4start

    def lookup(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.version == 4:
            node, bits = self._v4_start(), 32
        else:
            if self.ip_version == 4:
                return None
            node, bits = 0, 128
        n = int(addr)
        for i in range(bits - 1, -1, -1):
            if node >= self.node_count:
                break
            node = self._record(node, (n >> i) & 1)
        if node == self.node_count or node < self.node_count:
            return None
        off = node - self.node_count - 16
        val, _ = self._decode(off, 0)
        return val

    # --- secțiunea de date ---
    def _decode(self, off, _depth, absolute=False):
        base = 0 if absolute else self.data_start
        b = self.buf
        p = base + off
        ctrl = b[p]; p += 1
        t = ctrl >> 5
        if t == 1:  # pointer
            ss = (ctrl >> 3) & 3
            v = ctrl & 7
            if ss == 0:
                ptr = (v << 8) | b[p]; p += 1
            elif ss == 1:
                ptr = ((v << 16) | (b[p] << 8) | b[p + 1]) + 2048; p += 2
            elif ss == 2:
                ptr = ((v << 24) | (b[p] << 16) | (b[p + 1] << 8) | b[p + 2]) + 526336; p += 3
            else:
                ptr = struct.unpack(">I", b[p:p + 4])[0]; p += 4
            val, _ = self._decode(ptr, _depth + 1, absolute=False) if not absolute else \
                self._decode(ptr + self.data_start, _depth + 1, absolute=True)
            return val, p - base
        if t == 0:
            t = 7 + b[p]; p += 1
        size = ctrl & 0x1F
        if size == 29:
            size = 29 + b[p]; p += 1
        elif size == 30:
            size = 285 + ((b[p] << 8) | b[p + 1]); p += 2
        elif size == 31:
            size = 65821 + ((b[p] << 16) | (b[p + 1] << 8) | b[p + 2]); p += 3
        if t == 2:
            return b[p:p + size].decode("utf-8", "replace"), p + size - base
        if t == 3:
            return struct.unpack(">d", b[p:p + 8])[0], p + 8 - base
        if t == 4:
            return bytes(b[p:p + size]), p + size - base
        if t in (5, 6, 9, 10):
            return int.from_bytes(b[p:p + size], "big"), p + size - base
        if t == 8:
            v = int.from_bytes(b[p:p + size], "big")
            if size == 4 and v & 0x80000000:
                v -= 1 << 32
            return v, p + size - base
        if t == 7:
            res = {}
            o = p - base
            for _ in range(size):
                k, o = self._decode(o, _depth + 1, absolute)
                v, o = self._decode(o, _depth + 1, absolute)
                res[k] = v
            return res, o
        if t == 11:
            res = []
            o = p - base
            for _ in range(size):
                v, o = self._decode(o, _depth + 1, absolute)
                res.append(v)
            return res, o
        if t == 14:
            return bool(size), p - base
        if t == 15:
            return struct.unpack(">f", b[p:p + 4])[0], p + 4 - base
        return None, p - base


class Geo:
    def __init__(self, data_dir):
        self.path = os.path.join(data_dir, "geoip-country.mmdb")
        self.db = None
        self.cache = {}
        self.load()

    def load(self):
        try:
            if os.path.isfile(self.path):
                self.db = MMDB(self.path)
                self.cache = {}
                log("GeoIP încărcat")
        except Exception as e:
            log(f"GeoIP invalid: {e}", "warn")

    def country(self, ip):
        if ip in self.cache:
            return self.cache[ip]
        c = ""
        try:
            a = ipaddress.ip_address(ip)
            if a.is_private or a.is_loopback:
                c = "LAN"
            elif self.db:
                r = self.db.lookup(ip)
                if isinstance(r, dict):
                    c = ((r.get("country") or r.get("registered_country") or {}).get("iso_code")) or ""
        except Exception:
            pass
        if len(self.cache) > 50000:
            self.cache.clear()
        self.cache[ip] = c
        return c

    def age_days(self):
        if not os.path.isfile(self.path):
            return None
        return (time.time() - os.path.getmtime(self.path)) / 86400

    def update(self):
        t = time.gmtime()
        months = [(t.tm_year, t.tm_mon)]
        months.append((t.tm_year - 1, 12) if t.tm_mon == 1 else (t.tm_year, t.tm_mon - 1))
        for y, m in months:
            url = f"https://download.db-ip.com/free/dbip-country-lite-{y}-{m:02d}.mmdb.gz"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "NtS-Radio"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = gzip.decompress(r.read())
                tmp = self.path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                MMDB(tmp)  # validare
                os.replace(tmp, self.path)
                log(f"GeoIP actualizat ({y}-{m:02d})")
                self.load()
                return True
            except Exception as e:
                log(f"GeoIP download {y}-{m:02d} eșuat: {e}", "warn")
        return False

    def update_async(self, force=False):
        age = self.age_days()
        if force or age is None or age > 35:
            threading.Thread(target=self.update, daemon=True).start()
