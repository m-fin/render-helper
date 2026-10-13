#!/usr/bin/env python3
"""Mac side: pull ffmpeg jobs from m-fin/render-helper, run them locally, send results back.

Secrets come from the macOS Keychain:
  security find-generic-password -s macrenderhelper-github -w
  security find-generic-password -s macrenderhelper-hmac -w
"""
import argparse, datetime, json, os, platform, resource, shutil, socket, subprocess, sys, tempfile, threading, time, uuid
from rh_xfer import file_sha256, purge_old, serve_tailscale
from rh_common import (GitHub, Conflict, verify_job, validate_job, rewrite_args,
                       prefer_videotoolbox, ValidationError, post_args, REPO, sign_obj,
                       estimate_cloud_cpu_minutes, HEARTBEAT_EVERY)

HOME_DIR = os.path.expanduser("~/Library/Application Support/macrenderhelper")
XFER_DIR = os.path.join(HOME_DIR, "xfer")  # per-job folders for the Tailscale file endpoint

POLL = 15
MAX_INPUT_TOTAL = 4 * 1024**3
MAX_OUTPUT_EACH = 1900 * 1024**2
TIMEOUT = 2 * 3600


def keychain(service):
    return subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                          check=True, capture_output=True, text=True).stdout.strip()


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"


def _out(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def mac_info(ffmpeg_dir):
    """Cheap facts only (sysctl/sw_vers/ffmpeg -version); never renders."""
    ff = _out([os.path.join(ffmpeg_dir, "ffmpeg"), "-hide_banner", "-version"]).splitlines()
    return {"model": _out(["sysctl", "-n", "hw.model"]),
            "macos": _out(["sw_vers", "-productVersion"]),
            "chip": _out(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "arch": platform.machine(),
            "ffmpeg": ff[0] if ff else ""}


def worker_id(home=HOME_DIR):
    """Stable id, created once and kept in the install dir."""
    path = os.path.join(home, "worker_id")
    if os.path.isfile(path):
        with open(path) as f:
            wid = f.read().strip()
        if wid:
            return wid
    os.makedirs(home, exist_ok=True)
    wid = "mac-" + uuid.uuid4().hex[:12]
    with open(path, "w") as f:
        f.write(wid)
    return wid


def ensure_hello(gh, wid, key, info):
    """Pairing: commit workers/<id>/hello.json once (create-only). Returns True if written now."""
    path = f"workers/{wid}/hello.json"
    existing, _ = gh.get_file(path)
    if existing:
        return False
    hello = sign_obj({"type": "hello", "worker_id": wid, "at": now(), **info}, key)
    try:
        gh.put_file(path, hello, f"pair worker {wid}")
        return True
    except Conflict:
        return False


def send_heartbeat(gh, wid, key, info, state="idle", job=None, release=None):
    """Replace heartbeat-<id>.json on the 'worker-status' release. No commit."""
    hb = sign_obj({"type": "heartbeat", "worker_id": wid, "at": now(), "state": state,
                   "job": job, "model": info.get("model"), "ffmpeg": info.get("ffmpeg")}, key)
    rel = release or gh.status_release()
    gh.replace_asset(rel, f"heartbeat-{wid}.json", json.dumps(hb).encode())
    return hb


def append_stats(gh, rec, path="stats/jobs.jsonl"):
    """Append one line to stats/jobs.jsonl (retries on a sha race)."""
    import base64
    for _ in range(3):
        r = gh.req("GET", f"/contents/{path}")
        old = base64.b64decode(r["content"]).decode() if r else ""
        body = {"message": f"stats {rec.get('job_id')}",
                "content": base64.b64encode((old + json.dumps(rec, sort_keys=True) + "\n").encode()).decode()}
        if r:
            body["sha"] = r["sha"]
        try:
            return gh.req("PUT", f"/contents/{path}", body)
        except Conflict:
            continue


def try_claim(gh, job_id, worker):
    """Atomic claim: create claimed.json only if it doesn't exist (no sha => create-only).
    A concurrent creator makes GitHub return 409/422 -> Conflict -> skip."""
    path = f"jobs/{job_id}/claimed.json"
    existing, _ = gh.get_file(path)
    if existing:
        return False
    try:
        gh.put_file(path, {"worker": worker, "at": now()}, f"claim {job_id}")
        return True
    except Conflict:
        return False


def pending_jobs(gh):
    for d in gh.list_dir("jobs"):
        if d.get("type") != "dir":
            continue
        names = {f["name"] for f in gh.list_dir(f"jobs/{d['name']}")}
        if "job.json" in names and not names & {"claimed.json", "done.json", "failed.json"}:
            yield d["name"]


CAPTURE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rh_capture.mjs")


def capture_args(job, ind, outd, ffmpeg_dir, node_dir):
    """Fixed command: node <installed rh_capture.mjs> on the hash-checked, safely extracted bundle."""
    import hashlib
    from rh_common import safe_extract
    c = job["capture"]
    tar = os.path.join(ind, c["bundle"])
    with open(tar, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != c["bundle_sha256"]:
            raise ValidationError("bundle sha256 mismatch")
    game = os.path.join(ind, "game")
    os.makedirs(game)
    safe_extract(tar, game)
    node = os.path.join(node_dir, "node")
    args = [node, CAPTURE_SCRIPT, game, str(c["seed"]), str(c["w"]), str(c["h"]),
            os.path.join(outd, c["out"]), str(c["limit"]), os.path.join(ffmpeg_dir, "ffmpeg")]
    env = {"PATH": f"{node_dir}:{ffmpeg_dir}:/usr/bin:/bin", "HOME": os.path.expanduser("~")}
    return args, env


def _timed(args, env, cwd):
    ru0, t0 = resource.getrusage(resource.RUSAGE_CHILDREN), time.monotonic()
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
        code, tail = p.returncode, (p.stderr or "") + (p.stdout or "")
    except subprocess.TimeoutExpired:
        code, tail = -9, "timed out"
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    return code, tail, round(time.monotonic() - t0, 3), (round(cpu, 3) if cpu > 0 else None)


def run_bench(args, env, work, ind, outd, ffmpeg_dir):
    """capture-bench: capture lossless MKV locally, run 3 fixed encodes, write bench_stats.json, delete MKV."""
    mkv = os.path.join(ind, "bench.mkv")
    args = list(args); args[6] = mkv  # capture output -> local lossless intermediate (never uploaded)
    res, tails = {}, []
    try:
        code, tail, wall, cpu = _timed(args, env, work); tails.append(tail)
        res["capture_ffv1"] = {"exit": code, "wall_s": wall, "cpu_s": cpu,
                               "bytes": os.path.getsize(mkv) if os.path.isfile(mkv) else 0}
        if code != 0:
            return code, "\n".join(tails)
        ff = [os.path.join(ffmpeg_dir, "ffmpeg"), "-nostdin", "-y", "-loglevel", "error", "-i", mkv]
        tail_opts = ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        plan = [("x264_t2", x264 + ["-threads", "2"]), ("x264_all", x264), ("vt", None)]
        for name, opts in plan:
            out = os.path.join(outd, f"bench_{name}.mp4")
            if opts is None:  # bitrate matched to the first output's size
                dur = float(_out([os.path.join(ffmpeg_dir, "ffprobe"), "-v", "error", "-show_entries",
                                  "format=duration", "-of", "csv=p=0", mkv]) or 0) or 1
                opts = ["-c:v", "h264_videotoolbox", "-b:v",
                        str(int(res["x264_t2"]["bytes"] * 8 / dur))]
            code, tail, wall, cpu = _timed(ff + opts + tail_opts + [out], env, work); tails.append(tail)
            res[name] = {"exit": code, "wall_s": wall, "cpu_s": cpu, "args": opts,
                         "bytes": os.path.getsize(out) if os.path.isfile(out) else 0}
            if code != 0:
                return code, "\n".join(tails)
        with open(os.path.join(outd, "bench_stats.json"), "w") as f:
            json.dump({"measured_on": "mac", "results": res}, f, indent=1)
        return 0, json.dumps(res) + "\n" + "\n".join(tails)
    finally:
        if os.path.exists(mkv):
            os.remove(mkv)


def run_job(gh, job_id, key, ffmpeg_dir, vt=True, log=print, worker="", node_dir="/opt/homebrew/bin",
            xfer_dir=None):
    job, _ = gh.get_file(f"jobs/{job_id}/job.json")
    tag = f"job-{job_id}"
    stats, out_sha = {}, {}
    def finish(name, code, tail):
        doc = {"exit_code": code, "log_tail": tail[-4000:], "at": now()}
        if stats:
            doc["stats"] = stats
        if out_sha:  # tailscale: signed output hashes so the box can verify what it pulls
            doc = sign_obj(dict(doc, outputs_sha256=out_sha), key)
        gh.put_file(f"jobs/{job_id}/{name}", doc, f"{name} {job_id}")
    if not job or not verify_job(job, key):
        return finish("failed.json", -1, "bad or missing HMAC signature; refused")
    try:
        validate_job(job)
        if job["id"] != job_id:
            raise ValidationError("id mismatch")
    except ValidationError as e:
        return finish("failed.json", -1, f"rejected: {e}")
    ts = job.get("transport") == "tailscale"
    xjob = os.path.join(xfer_dir or XFER_DIR, job_id)
    rel = None if ts else gh.find_release(tag)
    if not ts and not rel:
        return finish("failed.json", -1, "release with inputs not found")
    work = tempfile.mkdtemp(prefix=f"rh-{job_id}-")
    ind, outd = os.path.join(work, "in"), os.path.join(work, "out")
    os.makedirs(ind); os.makedirs(outd)
    try:
        total = 0
        assets = {a["name"]: a for a in rel.get("assets", [])} if rel else {}
        for n in (job["inputs"] if ts else []):
            src = os.path.join(xjob, "in", n)
            if not os.path.isfile(src) or os.path.islink(src):
                return finish("failed.json", -1, f"missing input {n} (tailscale)")
            total += os.path.getsize(src)
            if total > MAX_INPUT_TOTAL:
                return finish("failed.json", -1, "inputs over size cap")
            if file_sha256(src) != job["input_sha256"][n]:
                return finish("failed.json", -1, f"input {n} sha256 mismatch; refused")
            shutil.move(src, os.path.join(ind, n))
        for n in ([] if ts else job["inputs"]):
            if n not in assets:
                return finish("failed.json", -1, f"missing input {n}")
            total += assets[n].get("size", 0)
            if total > MAX_INPUT_TOTAL:
                return finish("failed.json", -1, "inputs over size cap")
            with open(os.path.join(ind, n), "wb") as f:
                f.write(gh.download_asset(assets[n]))
        env = None
        if job["cmd"] == ["capture"]:
            try:
                args, env = capture_args(job, ind, outd, ffmpeg_dir, node_dir)
            except (ValidationError, OSError) as e:
                return finish("failed.json", -1, f"capture rejected: {e}")
        else:
            binpath = os.path.join(ffmpeg_dir, job["cmd"][0])
            args = prefer_videotoolbox(rewrite_args(job["cmd"], ind, outd, binpath),
                                       vt and not job.get("exact_encoder", False))
            if job["cmd"][0] == "ffmpeg":
                args.insert(1, "-nostdin")
        log(f"running {job_id}: {args}")
        ru0, t0 = resource.getrusage(resource.RUSAGE_CHILDREN), time.monotonic()
        if job.get("capture", {}).get("bench") == 1:
            code, tail = run_bench(args, env, work, ind, outd, ffmpeg_dir)
        else:
          try:
            p = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=TIMEOUT, shell=False, env=env)
            code, tail = p.returncode, (p.stderr or "") + (p.stdout or "")
            sw = [x.replace("h264_videotoolbox", "libx264").replace("hevc_videotoolbox", "libx265") for x in args]
            if code != 0 and sw != args:  # retry: VideoToolbox busy/unavailable -> software encode
                log(f"{job_id}: VideoToolbox failed, retrying with software encoder")
                p = subprocess.run(sw, cwd=work, capture_output=True, text=True, timeout=TIMEOUT, shell=False, env=env)
                code, tail = p.returncode, tail[-1500:] + "\n[retry software]\n" + (p.stderr or "") + (p.stdout or "")
          except subprocess.TimeoutExpired:
            code, tail = -9, "timed out"
        post = job.get("post")
        if code == 0 and post:
            for n in [n for n in job["outputs"] if n.endswith((".mp4", ".mov", ".mkv"))][:1]:
                fp, tmp = os.path.join(outd, n), os.path.join(outd, "post-" + n)
                aud = os.path.join(ind, post["audio"]) if post.get("audio") else None
                q = subprocess.run(post_args(os.path.join(ffmpeg_dir, "ffmpeg"), fp, aud, post.get("lufs"), tmp),
                                   capture_output=True, text=True, timeout=TIMEOUT)
                if q.returncode == 0:
                    os.replace(tmp, fp); tail += f"\n[post ok: audio={bool(aud)} lufs={post.get('lufs')}]"
                else:
                    code, tail = q.returncode, tail + "\n[post failed]\n" + q.stderr[-1500:]
        wall = time.monotonic() - t0
        ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
        cpu = round(cpu, 3) if cpu > 0 else None  # None = not measured
        out_bytes = sum(os.path.getsize(os.path.join(outd, n)) for n in job["outputs"]
                        if os.path.isfile(os.path.join(outd, n)))
        if code == 0:
            for n in job["outputs"]:
                fp = os.path.join(outd, n)
                if not os.path.isfile(fp):
                    code, tail = -2, tail + f"\nmissing output {n}"; break
                if os.path.getsize(fp) > MAX_OUTPUT_EACH:
                    code, tail = -3, tail + f"\noutput {n} over size cap"; break
                if ts:
                    os.makedirs(os.path.join(xjob, "out"), exist_ok=True)
                    out_sha[n] = file_sha256(fp)
                    shutil.move(fp, os.path.join(xjob, "out", n))
                else:
                    with open(fp, "rb") as f:
                        gh.upload_asset(rel, n, f.read())
        for n in job["inputs"]:
            if n in assets:
                gh.delete_asset(assets[n])
        stats.update({"job_id": job_id, "bot": job["bot"], "worker": worker, "finished": now(),
                      "exit_code": code, "wall_s": round(wall, 3), "cpu_s": cpu,
                      "input_bytes": total, "output_bytes": out_bytes,
                      "cloud_cpu_minutes_saved_estimate": estimate_cloud_cpu_minutes(cpu, wall)})
        bpath = os.path.join(outd, "bench_stats.json")
        if os.path.isfile(bpath):  # capture-bench: keep per-encode wall/cpu/bytes in the stats line too
            with open(bpath) as f:
                stats["bench"] = {k: {x: v.get(x) for x in ("exit", "wall_s", "cpu_s", "bytes")}
                                  for k, v in json.load(f).get("results", {}).items()}
        finish("done.json" if code == 0 else "failed.json", code, tail)
        try:
            append_stats(gh, stats)
        except Exception as e:
            log(f"stats append failed: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def selftest(ffmpeg_dir, vt=True):
    """Run a tiny 1s 64x64 testsrc job locally through the same validate/rewrite path. No GitHub."""
    from rh_common import sign_job
    key = b"selftest"
    job = sign_job({"id": "selftest", "bot": "installer", "created": now(),
                    "cmd": ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64:r=10",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "{out:t.mp4}"],
                    "inputs": [], "outputs": ["t.mp4"]}, key)
    assert verify_job(job, key)
    validate_job(job)
    work = tempfile.mkdtemp(prefix="rh-selftest-")
    try:
        args = prefer_videotoolbox(rewrite_args(job["cmd"], work, work, os.path.join(ffmpeg_dir, "ffmpeg")), vt)
        p = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if p.returncode != 0 and vt:  # VideoToolbox unavailable (e.g. VM): retry with software
            return selftest(ffmpeg_dir, False)
        ok = p.returncode == 0 and os.path.getsize(os.path.join(work, "t.mp4")) > 0
        print("PASS" if ok else "FAIL\n" + p.stderr[-2000:])
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--ffmpeg-dir", default="/opt/homebrew/bin")
    ap.add_argument("--node-dir", default="/opt/homebrew/bin")
    ap.add_argument("--no-videotoolbox", action="store_true")
    ap.add_argument("--once", action="store_true", help="check once and exit (test)")
    ap.add_argument("--selftest", action="store_true", help="tiny local test render, prints PASS/FAIL")
    ap.add_argument("--xfer", action="store_true",
                    help="serve signed job files on this Mac's Tailscale 100.x address, port 8765 only")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.ffmpeg_dir, not a.no_videotoolbox)
    if sys.platform != "darwin":
        print("Mac Render Helper runs on macOS only.", file=sys.stderr)
        return 2
    gh = GitHub(keychain("macrenderhelper-github"), a.repo)
    key = keychain("macrenderhelper-hmac").encode()
    worker = worker_id()
    info = mac_info(a.ffmpeg_dir)
    print(f"Mac Render Helper worker {worker} ({info['model']}, {info['ffmpeg']}) polling {a.repo}", flush=True)
    if a.xfer and not a.once:
        threading.Thread(target=serve_tailscale, args=(key, XFER_DIR),
                         kwargs={"log": lambda m: print(m, flush=True)}, daemon=True).start()
    state = {"state": "idle", "job": None}
    def beat_forever():  # own thread so long renders still show 'busy', not offline
        while True:
            try:
                send_heartbeat(gh, worker, key, info, state["state"], state["job"])
            except Exception as e:
                print(f"heartbeat error: {e}", file=sys.stderr, flush=True)
            time.sleep(HEARTBEAT_EVERY)
    paired = False
    while True:
        try:
            if not paired:
                if ensure_hello(gh, worker, key, info):
                    print(f"paired: committed workers/{worker}/hello.json", flush=True)
                paired = True
                if not a.once:
                    threading.Thread(target=beat_forever, daemon=True).start()
                else:
                    send_heartbeat(gh, worker, key, info)
            for jid in pending_jobs(gh):
                if try_claim(gh, jid, worker):
                    state.update(state="busy", job=jid)
                    try:
                        run_job(gh, jid, key, a.ffmpeg_dir, not a.no_videotoolbox,
                                log=lambda m: print(m, flush=True), worker=worker,
                                node_dir=a.node_dir)
                        purge_old(XFER_DIR)  # unpulled tailscale files older than 24h
                    finally:
                        state.update(state="idle", job=None)
        except Exception as e:  # keep the agent alive through network blips
            print(f"error: {e}", file=sys.stderr, flush=True)
        if a.once:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
