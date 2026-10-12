"""Signed file transfer over Tailscale for Mac Render Helper (Python 3 stdlib only).

Mac side:  XferServer binds ONLY to the Mac's Tailscale 100.x address (from `tailscale ip -4`) on 8765.
Box side:  XferClient talks HTTP/1.1 through `tailscale nc` (userspace networking), SOCKS5, or direct.

Every request carries X-RH-TS, X-RH-Nonce, X-RH-SHA256 (body hash) and X-RH-Sig =
HMAC-SHA256(RH_HMAC_KEY, "RHX1\\n" METHOD \\n PATH \\n TS \\n NONCE \\n BODY_SHA256).
Requests outside a 5 min window or with a reused nonce are refused. GET responses are signed
the same way (bound to the request nonce) so the box can trust downloaded outputs.
Files live in <root>/<job_id>/{in,out}/<name>; names pass rh_common.check_name (no '/', no '..').
"""
import hashlib, hmac, http.client, http.server, ipaddress, os, re, shutil, socket, struct, subprocess
import threading, time, uuid
from rh_common import check_name, ValidationError

PORT = 8765
WINDOW = 300
MAX_FILE = 1900 * 1024**2
MAX_JOB_TOTAL = 4 * 1024**3
CGNAT = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_BINS = ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                  "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale")
PATH_RE = re.compile(r"^/v1/jobs/([^/]+)/(in|out)/([^/]+)$|^/v1/jobs/([^/]+)$|^/v1/ping$")
CHUNK = 1024 * 1024


def sign_req(key, method, path, ts, nonce, body_sha):
    msg = "\n".join(["RHX1", method, path, str(ts), nonce, body_sha]).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def sign_resp(key, path, nonce, body_sha, status):
    msg = "\n".join(["RHX1-RESP", path, nonce, body_sha, str(status)]).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def tailscale_ipv4(run=subprocess.run):
    """Return the Tailscale 100.x address or raise RuntimeError. Never falls back to another interface."""
    last = None
    for b in TAILSCALE_BINS:
        try:
            p = run([b, "ip", "-4"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = e; continue
        if p.returncode != 0:
            last = p.stderr.strip(); continue
        for line in p.stdout.split():
            try:
                ip = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if ip.version == 4 and ip in CGNAT:
                return str(ip)
            raise RuntimeError(f"tailscale ip {ip} is not in 100.64.0.0/10; refusing to start")
    raise RuntimeError(f"no Tailscale IPv4 address ({last}); refusing to start")


class NonceCache:
    def __init__(self):
        self.seen, self.lock = {}, threading.Lock()

    def add(self, nonce, now):
        with self.lock:
            for n, t in list(self.seen.items()):
                if t < now - 2 * WINDOW:
                    del self.seen[n]
            if nonce in self.seen:
                return False
            self.seen[nonce] = now
            return True


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "rh-xfer/1"

    def log_message(self, fmt, *a):  # no headers/sigs in logs
        if self.server.log:
            self.server.log(f"xfer {self.address_string()} {self.command} {self.path.split('?')[0]} {a[1] if len(a) > 1 else ''}")

    def _send(self, code, body=b"", nonce="", path=""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        sha = hashlib.sha256(body).hexdigest()
        self.send_header("X-RH-SHA256", sha)
        self.send_header("X-RH-Sig", sign_resp(self.server.key, path, nonce, sha, code))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _auth(self, method):
        h = self.headers
        ts, nonce, sha, sig = h.get("X-RH-TS", ""), h.get("X-RH-Nonce", ""), h.get("X-RH-SHA256", ""), h.get("X-RH-Sig", "")
        if not (ts.isdigit() and re.fullmatch(r"[0-9a-f]{32}", nonce) and re.fullmatch(r"[0-9a-f]{64}", sha)
                and re.fullmatch(r"[0-9a-f]{64}", sig)):
            return None
        good = sign_req(self.server.key, method, self.path, int(ts), nonce, sha)
        if not hmac.compare_digest(sig, good):
            return None
        now = self.server.clock()
        if abs(now - int(ts)) > WINDOW or not self.server.nonces.add(nonce, now):
            return None
        return nonce, sha

    def _route(self):
        m = PATH_RE.match(self.path)
        if not m:
            raise ValidationError("bad path")
        if self.path == "/v1/ping":
            return None, None, None
        jid = check_name(m.group(1) or m.group(4))
        if m.group(1):
            return jid, m.group(2), check_name(m.group(3))
        return jid, None, None

    def _job_dir(self, jid):
        d = os.path.realpath(os.path.join(self.server.root, jid))
        if os.path.dirname(d) != os.path.realpath(self.server.root):
            raise ValidationError("escape")
        return d

    def _handle(self, method):
        try:
            route = self._route()
        except ValidationError:
            self.close_connection = True
            return self._send(404)
        clen = self.headers.get("Content-Length", "0")
        if method != "PUT" and clen not in ("", "0"):
            self.close_connection = True
            return self._send(400)
        a = self._auth(method)
        if not a:
            self.close_connection = True
            return self._send(401)
        nonce, sha = a
        jid, kind, name = route
        try:
            if method == "GET" and jid is None:
                return self._send(200, b"pong", nonce, self.path)
            if method == "PUT" and kind == "in" and name:
                return self._put(jid, name, sha, nonce)
            if method == "GET" and kind == "out" and name:
                return self._get(jid, name, nonce)
            if method == "DELETE" and kind is None:
                shutil.rmtree(self._job_dir(jid), ignore_errors=True)
                return self._send(200, b"deleted", nonce, self.path)
        except ValidationError:
            self.close_connection = True
            return self._send(400, b"", nonce, self.path)
        self.close_connection = True
        return self._send(405, b"", nonce, self.path)

    def _put(self, jid, name, sha, nonce):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self._send(411, b"", nonce, self.path)
        d = os.path.join(self._job_dir(jid), "in")
        os.makedirs(d, exist_ok=True)
        used = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d) if f != name and not f.startswith("."))
        if n < 0 or n > self.server.max_file or used + n > self.server.max_job:
            self.close_connection = True
            return self._send(413, b"", nonce, self.path)
        tmp = os.path.join(d, f".{name}.{uuid.uuid4().hex}.part")
        h, left = hashlib.sha256(), n
        try:
            with open(tmp, "wb") as f:
                while left:
                    b = self.rfile.read(min(CHUNK, left))
                    if not b:
                        raise ValidationError("short body")
                    h.update(b); f.write(b); left -= len(b)
            if not hmac.compare_digest(h.hexdigest(), sha):
                raise ValidationError("sha256 mismatch")
            os.replace(tmp, os.path.join(d, name))
        except (ValidationError, OSError):
            if os.path.exists(tmp):
                os.remove(tmp)
            self.close_connection = True
            return self._send(422, b"", nonce, self.path)
        return self._send(200, sha.encode(), nonce, self.path)

    def _get(self, jid, name, nonce):
        fp = os.path.join(self._job_dir(jid), "out", name)
        if not os.path.isfile(fp) or os.path.islink(fp):
            return self._send(404, b"", nonce, self.path)
        h = hashlib.sha256()
        with open(fp, "rb") as f:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
        sha = h.hexdigest()
        self.send_response(200)
        self.send_header("Content-Length", str(os.path.getsize(fp)))
        self.send_header("X-RH-SHA256", sha)
        self.send_header("X-RH-Sig", sign_resp(self.server.key, self.path, nonce, sha, 200))
        self.end_headers()
        with open(fp, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    def do_GET(self): self._handle("GET")
    def do_PUT(self): self._handle("PUT")
    def do_DELETE(self): self._handle("DELETE")


class XferServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, key, root, log=None, clock=time.time, allow_any_addr=False):
        if not allow_any_addr and ipaddress.ip_address(addr[0]) not in CGNAT:
            raise RuntimeError(f"refusing to bind {addr[0]}: not a Tailscale 100.64.0.0/10 address")
        if addr[0] in ("0.0.0.0", "", "::"):
            raise RuntimeError("refusing to bind all interfaces")
        self.key, self.root, self.log, self.clock = key, root, log, clock
        self.nonces, self.max_file, self.max_job = NonceCache(), MAX_FILE, MAX_JOB_TOTAL
        os.makedirs(root, exist_ok=True)
        super().__init__(addr, Handler)


def serve_tailscale(key, root, log=print, port=PORT, retry=30):
    """Wait until Tailscale has a 100.x address, then serve on it forever. Never binds elsewhere."""
    while True:
        try:
            ip = tailscale_ipv4()
            srv = XferServer((ip, port), key, root, log)
            log(f"xfer: serving {root} on {ip}:{port} (Tailscale only)")
            srv.serve_forever()
        except Exception as e:
            log(f"xfer: not serving ({e}); retrying in {retry}s")
        time.sleep(retry)


def purge_old(root, max_age=24 * 3600, now=None):
    now = now or time.time()
    if not os.path.isdir(root):
        return
    for d in os.listdir(root):
        p = os.path.join(root, d)
        if os.path.isdir(p) and now - os.path.getmtime(p) > max_age:
            shutil.rmtree(p, ignore_errors=True)


# ---------- client (box side) ----------
class _NcConnection(http.client.HTTPConnection):
    """HTTP over `tailscale nc HOST PORT` (works with userspace networking)."""
    def __init__(self, host, port, timeout=60, nc=("tailscale", "nc")):
        super().__init__(host, port, timeout=timeout); self.nc, self.proc = nc, None

    def connect(self):
        a, b = socket.socketpair()
        self.proc = subprocess.Popen(list(self.nc) + [self.host, str(self.port)], stdin=b, stdout=b,
                                     stderr=subprocess.DEVNULL)
        b.close(); a.settimeout(self.timeout); self.sock = a

    def close(self):
        super().close()
        if self.proc:
            try:
                self.proc.terminate(); self.proc.wait(5)
            except Exception:
                pass
            self.proc = None


class _SocksConnection(http.client.HTTPConnection):
    def __init__(self, host, port, proxy, timeout=60):
        super().__init__(host, port, timeout=timeout); self.proxy = proxy

    def connect(self):
        ph, pp = self.proxy.rsplit(":", 1)
        s = socket.create_connection((ph, int(pp)), self.timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            raise OSError("socks5: no-auth refused")
        s.sendall(b"\x05\x01\x00\x01" + socket.inet_aton(self.host) + struct.pack(">H", self.port))
        r = s.recv(10)
        if len(r) < 2 or r[1] != 0:
            raise OSError("socks5: connect failed")
        self.sock = s


class XferError(Exception):
    pass


class XferClient:
    def __init__(self, key, host="100.78.67.27", port=PORT, mode=None, timeout=60, clock=time.time):
        self.key, self.host, self.port, self.timeout, self.clock = key, host, port, timeout, clock
        self.mode = mode or ("socks:" + os.environ["RH_XFER_SOCKS"] if os.environ.get("RH_XFER_SOCKS") else "nc")

    def _conn(self):
        if self.mode == "direct":
            return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        if self.mode.startswith("socks:"):
            return _SocksConnection(self.host, self.port, self.mode[6:], self.timeout)
        return _NcConnection(self.host, self.port, self.timeout)

    def _req(self, method, path, body_path=None, body_sha=EMPTY_SHA, out_path=None):
        ts, nonce = int(self.clock()), uuid.uuid4().hex
        hdrs = {"X-RH-TS": str(ts), "X-RH-Nonce": nonce, "X-RH-SHA256": body_sha,
                "X-RH-Sig": sign_req(self.key, method, path, ts, nonce, body_sha), "Connection": "close"}
        c = self._conn()
        try:
            if body_path:
                hdrs["Content-Length"] = str(os.path.getsize(body_path))
                c.putrequest(method, path)
                for k, v in hdrs.items():
                    c.putheader(k, v)
                c.endheaders()
                with open(body_path, "rb") as f:
                    for b in iter(lambda: f.read(CHUNK), b""):
                        c.send(b)
            else:
                c.request(method, path, headers=hdrs)
            r = c.getresponse()
            rsha, rsig = r.getheader("X-RH-SHA256", ""), r.getheader("X-RH-Sig", "")
            if not hmac.compare_digest(rsig, sign_resp(self.key, path, nonce, rsha, r.status)):
                raise XferError(f"{method} {path}: bad response signature (HTTP {r.status})")
            if r.status != 200:
                r.read(); raise XferError(f"{method} {path}: HTTP {r.status}")
            if out_path:
                h, tmp = hashlib.sha256(), out_path + ".part"
                with open(tmp, "wb") as f:
                    for b in iter(lambda: r.read(CHUNK), b""):
                        h.update(b); f.write(b)
                if not hmac.compare_digest(h.hexdigest(), rsha):
                    os.remove(tmp); raise XferError(f"{path}: sha256 mismatch on download")
                os.replace(tmp, out_path)
                return rsha
            data = r.read()
            if hashlib.sha256(data).hexdigest() != rsha:
                raise XferError("response body hash mismatch")
            return data
        finally:
            c.close()

    def ping(self):
        try:
            return self._req("GET", "/v1/ping") == b"pong"
        except Exception:
            return False

    def put(self, job_id, path):
        name = check_name(os.path.basename(path))
        sha = file_sha256(path)
        got = self._req("PUT", f"/v1/jobs/{check_name(job_id)}/in/{name}", body_path=path, body_sha=sha)
        if got.decode() != sha:
            raise XferError("upload hash not confirmed")
        return sha

    def get(self, job_id, name, dest):
        return self._req("GET", f"/v1/jobs/{check_name(job_id)}/out/{check_name(name)}", out_path=dest)

    def delete(self, job_id):
        return self._req("DELETE", f"/v1/jobs/{check_name(job_id)}")


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()
