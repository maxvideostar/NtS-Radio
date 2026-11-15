import asyncio
import json
import mimetypes
import os
import urllib.parse
from http.cookies import SimpleCookie

STATUS = {200: "OK", 201: "Created", 204: "No Content", 301: "Moved Permanently", 302: "Found",
          304: "Not Modified", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
          404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
          429: "Too Many Requests", 500: "Internal Server Error", 503: "Service Unavailable"}


class HTTPError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(msg)
        self.code = code
        self.msg = msg or STATUS.get(code, "")


class Request:
    def __init__(self, reader, writer, method, target, version, headers, ip):
        self.reader = reader
        self.writer = writer
        self.method = method.upper()
        self.version = version
        self.headers = headers
        self.ip = ip
        parsed = urllib.parse.urlsplit(target)
        self.path = urllib.parse.unquote(parsed.path) or "/"
        self.raw_query = parsed.query
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        self.cookies = {}
        if "cookie" in headers:
            c = SimpleCookie()
            try:
                c.load(headers["cookie"])
                self.cookies = {k: m.value for k, m in c.items()}
            except Exception:
                pass
        self.user = None
        self.taken = False   # handler-ul a preluat conexiunea (streaming)

    async def body(self, limit=2 * 1024 * 1024):
        n = int(self.headers.get("content-length") or 0)
        if n > limit:
            raise HTTPError(413)
        if n <= 0:
            return b""
        return await self.reader.readexactly(n)

    async def json(self):
        b = await self.body()
        if not b:
            return {}
        try:
            return json.loads(b)
        except Exception:
            raise HTTPError(400, "JSON invalid")


async def send(writer, code=200, body=b"", ctype="text/plain; charset=utf-8", headers=None):
    if isinstance(body, str):
        body = body.encode()
    headers = headers or {}
    h = [f"HTTP/1.1 {code} {STATUS.get(code, '')}", f"Content-Type: {ctype}",
         f"Content-Length: {len(body)}", "Connection: close"]
    if "Cache-Control" not in headers:
        h.append("Cache-Control: no-store")
    for k, v in headers.items():
        h.append(f"{k}: {v}")
    writer.write(("\r\n".join(h) + "\r\n\r\n").encode() + body)
    try:
        await asyncio.wait_for(writer.drain(), 30)
    except Exception:
        pass


async def send_json(writer, obj, code=200, headers=None):
    await send(writer, code, json.dumps(obj, ensure_ascii=False, default=str), "application/json; charset=utf-8",
               headers)


async def send_file(writer, path, extra_headers=None):
    if not os.path.isfile(path):
        raise HTTPError(404)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript",):
        ctype += "; charset=utf-8"
    with open(path, "rb") as f:
        data = f.read()
    h = {"Cache-Control": "no-cache"}
    h.update(extra_headers or {})
    await send(writer, 200, data, ctype, h)


class ChunkedReader:
    """citește un corp Transfer-Encoding: chunked ca flux continuu"""

    def __init__(self, reader):
        self.r = reader
        self.left = 0
        self.done = False

    async def read(self, n):
        if self.done:
            return b""
        if self.left == 0:
            line = await self.r.readline()
            if not line:
                self.done = True
                return b""
            try:
                self.left = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                self.done = True
                return b""
            if self.left == 0:
                self.done = True
                return b""
        data = await self.r.read(min(n, self.left))
        if not data:
            self.done = True
            return b""
        self.left -= len(data)
        if self.left == 0:
            await self.r.readline()
        return data


class Server:
    def __init__(self, app, host, port, trusted_proxies=("127.0.0.1", "::1")):
        self.app = app
        self.host = host
        self.port = port
        self.trusted = set(trusted_proxies)

    async def start(self):
        self.server = await asyncio.start_server(self.handle, self.host, self.port, limit=65536, backlog=1024)
        return self.server

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        req = None
        try:
            line = await asyncio.wait_for(reader.readline(), 15)
            if not line:
                return
            try:
                sline = line.decode("utf-8")
            except UnicodeDecodeError:
                sline = line.decode("latin-1")
            parts = sline.strip().split(" ")
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            version = parts[2] if len(parts) > 2 else "HTTP/1.0"
            headers = {}
            for _ in range(100):
                hl = await asyncio.wait_for(reader.readline(), 15)
                if hl in (b"\r\n", b"\n", b""):
                    break
                if b":" in hl:
                    k, v = hl.decode("latin-1").split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if ip in self.trusted:
                fwd = headers.get("x-real-ip") or headers.get("x-forwarded-for", "").split(",")[0].strip()
                if fwd:
                    ip = fwd
            req = Request(reader, writer, method, target, version, headers, ip)
            await self.app.dispatch(req)
        except HTTPError as e:
            if req and not req.taken:
                if req.path.startswith("/api/"):
                    await send_json(writer, {"error": e.msg}, e.code)
                else:
                    await send(writer, e.code, e.msg)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:  # noqa
            import traceback
            traceback.print_exc()
            if req and not req.taken:
                try:
                    await send_json(writer, {"error": "eroare internă: %s" % e}, 500)
                except Exception:
                    pass
        finally:
            try:
                writer.close()
            except Exception:
                pass
