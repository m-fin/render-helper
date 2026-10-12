"""Shared helpers for Mac Render Helper (Python 3 stdlib only)."""
import base64, hashlib, hmac, json, re, urllib.error, urllib.parse, urllib.request

REPO = "m-fin/render-helper"
API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
ALLOWED_BINARIES = ("ffmpeg", "ffprobe")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PLACEHOLDER_RE = re.compile(r"\{(in|out):([^}]*)\}")
PROTO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
MAX_ARGS = 400
MAX_ARG_LEN = 4096


class ValidationError(ValueError):
    pass


class Conflict(Exception):
    """Raised when a contents-API write loses a race (HTTP 409/422 sha mismatch)."""


# ---------- signing ----------
def canonical(job):
    body = {k: v for k, v in job.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign_job(job, key):
    """Sign any JSON object (jobs, hello.json, heartbeat.json)."""
    job = dict(job)
    job["sig"] = hmac.new(key, canonical(job), hashlib.sha256).hexdigest()
    return job


def verify_obj(obj, key):
    return verify_job(obj, key)


sign_obj = sign_job


def verify_job(job, key):
    sig = job.get("sig")
    if not isinstance(sig, str):
        return False
    good = hmac.new(key, canonical(job), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


# ---------- validation ----------
def check_name(n):
    if not isinstance(n, str) or not NAME_RE.match(n) or ".." in n:
        raise ValidationError(f"bad file name: {n!r}")
    return n


def validate_job(job):
    for k in ("id", "bot", "created", "cmd", "inputs", "outputs"):
        if k not in job:
            raise ValidationError(f"missing {k}")
    if "exact_encoder" in job and type(job["exact_encoder"]) is not bool:
        raise ValidationError("exact_encoder must be true/false")
    check_name(job["id"])
    inputs = [check_name(n) for n in job["inputs"]]
    outputs = [check_name(n) for n in job["outputs"]]
    if set(inputs) & set(outputs):
        raise ValidationError("name used as both input and output")
    validate_transport(job, inputs)
    cmd = job["cmd"]
    if cmd == ["capture"]:
        return validate_capture(job, inputs, outputs)
    if "capture" in job:
        raise ValidationError("capture block only allowed with cmd ['capture']")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        raise ValidationError("cmd must be a non-empty list of strings")
    if cmd[0] not in ALLOWED_BINARIES:
        raise ValidationError(f"binary not allowed: {cmd[0]!r}")
    if len(cmd) > MAX_ARGS:
        raise ValidationError("too many args")
    for a in cmd[1:]:
        validate_arg(a, inputs, outputs)
    return True


def validate_transport(job, inputs):
    """transport 'tailscale': inputs arrive over the signed Tailscale endpoint and their sha256
    is pinned in the signed job, so the worker can check them before running."""
    t = job.get("transport", "github")
    if t not in ("github", "tailscale"):
        raise ValidationError("transport must be github or tailscale")
    if t == "tailscale":
        hs = job.get("input_sha256")
        if not isinstance(hs, dict) or set(hs) != set(inputs) or not all(
                isinstance(v, str) and HEX64.match(v) for v in hs.values()):
            raise ValidationError("tailscale jobs need input_sha256 for every input")
    elif "input_sha256" in job:
        raise ValidationError("input_sha256 only for tailscale transport")


CAPTURE_KEYS = {"bundle", "bundle_sha256", "seed", "w", "h", "limit", "out"}
BENCH_OUTPUTS = ["bench_x264_t2.mp4", "bench_x264_all.mp4", "bench_vt.mp4", "bench_stats.json"]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def validate_capture(job, inputs, outputs):
    """Browser capture: runs ONLY the repo's fixed rh_capture.mjs. The job supplies numbers, one
    signed game bundle (tar, sha256 pinned in the signed job) and one output name. No URLs, no code args."""
    c = job.get("capture")
    if isinstance(c, dict) and c.get("bench") == 1 and set(c) == CAPTURE_KEYS | {"bench"}:
        if outputs != BENCH_OUTPUTS or c["out"] != BENCH_OUTPUTS[0]:
            raise ValidationError("bench outputs must be exactly " + ",".join(BENCH_OUTPUTS))
        outputs = [c["out"]]
    elif not isinstance(c, dict) or set(c) != CAPTURE_KEYS:
        raise ValidationError("capture needs exactly: " + ",".join(sorted(CAPTURE_KEYS)))
    if c["bundle"] not in inputs or not c["bundle"].endswith(".tar"):
        raise ValidationError("capture bundle must be a listed .tar input")
    if not isinstance(c["bundle_sha256"], str) or not HEX64.match(c["bundle_sha256"]):
        raise ValidationError("bad bundle_sha256")
    if c["out"] not in outputs or not c["out"].endswith((".mp4", ".mkv")) or len(outputs) != 1:
        raise ValidationError("capture out must be the single .mp4 or .mkv (lossless FFV1) output")
    for k, lo, hi in (("seed", 0, 2**31), ("w", 16, 3840), ("h", 16, 3840), ("limit", 0, 30 * 600)):
        if type(c[k]) is not int or not lo <= c[k] <= hi:
            raise ValidationError(f"capture {k} out of range")
    return True


def safe_extract(tar_path, dest):
    """Extract regular files/dirs only; no links, devices, absolute paths or '..'."""
    import os, tarfile
    with tarfile.open(tar_path) as t:
        for m in t.getmembers():
            n = m.name
            if n.startswith("/") or ".." in n.split("/") or not (m.isfile() or m.isdir()):
                raise ValidationError(f"unsafe tar member: {n!r}")
        for m in t.getmembers():
            target = os.path.join(dest, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with t.extractfile(m) as src, open(target, "wb") as dst:
                    dst.write(src.read())


def validate_arg(a, inputs, outputs):
    if len(a) > MAX_ARG_LEN or "\x00" in a:
        raise ValidationError("arg too long / NUL")
    for kind, name in PLACEHOLDER_RE.findall(a):
        if (kind == "in" and name not in inputs) or (kind == "out" and name not in outputs):
            raise ValidationError(f"unknown placeholder {{{kind}:{name}}}")
    bare = PLACEHOLDER_RE.sub("X", a)
    if bare.startswith("/") or bare.startswith("~"):
        raise ValidationError(f"absolute path not allowed: {a!r}")
    if ".." in bare:
        raise ValidationError(f"'..' not allowed: {a!r}")
    m = PROTO_RE.match(bare)
    if m and m.group(1).lower() != "file":
        raise ValidationError(f"protocol not allowed: {a!r}")
    if bare.lower().startswith("file:") and ("/" in bare[5:].lstrip("X") or bare[5:].startswith("/")):
        raise ValidationError(f"file: path must be local name: {a!r}")
    # filter-graph file loaders / sub-protocols hidden inside options
    low = bare.lower()
    for bad in ("concat:", "subfile,", "pipe:", "tcp:", "udp:", "http", "rtmp", "ftp:", "sendcmd", "movie=", "amovie=", "fd:", "data:"):
        if bad in low:
            raise ValidationError(f"forbidden token {bad!r} in {a!r}")


def rewrite_args(cmd, in_dir, out_dir, ffmpeg_path=None):
    """Replace {in:NAME}/{out:NAME} with paths inside the job dir."""
    def sub(m):
        kind, name = m.group(1), check_name(m.group(2))
        return f"{in_dir if kind == 'in' else out_dir}/{name}"
    out = [PLACEHOLDER_RE.sub(sub, a) for a in cmd[1:]]
    return [ffmpeg_path or cmd[0]] + out


def prefer_videotoolbox(args, enabled=True):
    if not enabled:
        return args
    return ["h264_videotoolbox" if a in ("libx264", "h264") else
            "hevc_videotoolbox" if a in ("libx265", "hevc") else a for a in args]


# ---------- GitHub REST ----------
class GitHub:
    def __init__(self, token, repo=REPO, opener=None):
        self.token, self.repo = token, repo
        self.opener = opener or urllib.request.urlopen

    def req(self, method, url, body=None, raw=None, ctype="application/json", accept="application/vnd.github+json"):
        if not url.startswith("http"):
            url = f"{API}/repos/{self.repo}{url}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(url, data=data, method=method)
        r.add_header("Authorization", f"Bearer {self.token}")
        r.add_header("Accept", accept)
        r.add_header("X-GitHub-Api-Version", "2022-11-28")
        r.add_header("User-Agent", "mac-render-helper")
        if data is not None:
            r.add_header("Content-Type", ctype)
        try:
            with self.opener(r) as resp:
                payload = resp.read()
                if accept == "application/octet-stream":
                    return payload
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and method == "PUT" and "/contents/" in url:
                raise Conflict(str(e))
            if e.code == 404:
                return None
            raise

    # contents
    def get_file(self, path):
        r = self.req("GET", f"/contents/{path}")
        if not r:
            return None, None
        return json.loads(base64.b64decode(r["content"])), r["sha"]

    def put_file(self, path, obj, message, sha=None):
        body = {"message": message, "content": base64.b64encode(json.dumps(obj, indent=1).encode()).decode()}
        if sha:
            body["sha"] = sha
        return self.req("PUT", f"/contents/{path}", body)

    def list_dir(self, path):
        r = self.req("GET", f"/contents/{path}")
        return r if isinstance(r, list) else []

    # releases
    def release_by_tag(self, tag):
        return self.req("GET", f"/releases/tags/{tag}")

    def find_release(self, tag):
        r = self.release_by_tag(tag)
        if r:
            return r
        for rel in self.req("GET", "/releases?per_page=100") or []:  # drafts aren't found by tag
            if rel.get("tag_name") == tag:
                return rel
        return None

    def create_release(self, tag, name):
        return self.req("POST", "/releases", {"tag_name": tag, "name": name, "prerelease": True,
                                               "body": "Mac Render Helper job (auto-deleted)"})

    def upload_asset(self, release, name, data):
        url = f"{UPLOADS}/repos/{self.repo}/releases/{release['id']}/assets?name={urllib.parse.quote(name)}"
        return self.req("POST", url, raw=data, ctype="application/octet-stream")

    def download_asset(self, asset):
        return self.req("GET", f"/releases/assets/{asset['id']}", accept="application/octet-stream")

    def delete_asset(self, asset):
        return self.req("DELETE", f"/releases/assets/{asset['id']}")

    def delete_release(self, release, tag):
        self.req("DELETE", f"/releases/{release['id']}")
        self.req("DELETE", f"/git/refs/tags/{tag}")

    # ---- worker status (heartbeat as a replaced release asset: no commits) ----
    def status_release(self, tag=None):
        tag = tag or STATUS_TAG
        rel = self.find_release(tag)
        if rel:
            return rel
        return self.req("POST", "/releases", {"tag_name": tag, "name": "worker status",
                                               "prerelease": True,
                                               "body": "Mac Render Helper heartbeats (asset replaced, never committed)"})

    def replace_asset(self, release, name, data):
        """Upload `name`, then delete older copies so exactly one remains."""
        old = [a for a in release.get("assets", []) if a.get("name") == name]
        for a in old:
            self.delete_asset(a)
        return self.upload_asset(release, name, data)


STATUS_TAG = "worker-status"
HEARTBEAT_EVERY = 60
OFFLINE_AFTER = 600


def utcnow_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def parse_iso(s):
    import datetime
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()


# Estimate only. Formula: measured Mac CPU seconds (ffmpeg user+sys) / 60 * CLOUD_CPU_FACTOR.
# Falls back to measured wall seconds / 60 when CPU time isn't available.
# CLOUD_CPU_FACTOR=1.0 assumes a cloud vCPU does the same work per CPU-second as the Mac
# (an assumption, not measured). VideoToolbox runs on the media engine, so CPU time
# understates the real work; the estimate is conservative there.
CLOUD_CPU_FACTOR = 1.0


def estimate_cloud_cpu_minutes(cpu_s, wall_s, factor=CLOUD_CPU_FACTOR):
    if cpu_s is not None:
        basis, secs = "cpu_s", cpu_s
    else:
        basis, secs = "wall_s", wall_s
    return {"value": round(secs / 60 * factor, 3), "is_estimate": True,
            "formula": f"{basis} / 60 * {factor} (CLOUD_CPU_FACTOR, assumed)", "basis": basis}
