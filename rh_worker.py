#!/usr/bin/env python3
"""Mac side: pull ffmpeg jobs from m-fin/render-helper, run them locally, send results back.

Secrets come from the macOS Keychain:
  security find-generic-password -s render-helper-github -w
  security find-generic-password -s render-helper-hmac -w
"""
import argparse, datetime, os, shutil, socket, subprocess, sys, tempfile, time
from rh_common import (GitHub, Conflict, verify_job, validate_job, rewrite_args,
                       prefer_videotoolbox, ValidationError, REPO)

POLL = 15
MAX_INPUT_TOTAL = 4 * 1024**3
MAX_OUTPUT_EACH = 1900 * 1024**2
TIMEOUT = 2 * 3600


def keychain(service):
    return subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                          check=True, capture_output=True, text=True).stdout.strip()


def now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z"


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


def run_job(gh, job_id, key, ffmpeg_dir, vt=True, log=print):
    job, _ = gh.get_file(f"jobs/{job_id}/job.json")
    tag = f"job-{job_id}"
    def finish(name, code, tail):
        gh.put_file(f"jobs/{job_id}/{name}", {"exit_code": code, "log_tail": tail[-4000:], "at": now()}, f"{name} {job_id}")
    if not job or not verify_job(job, key):
        return finish("failed.json", -1, "bad or missing HMAC signature; refused")
    try:
        validate_job(job)
        if job["id"] != job_id:
            raise ValidationError("id mismatch")
    except ValidationError as e:
        return finish("failed.json", -1, f"rejected: {e}")
    rel = gh.find_release(tag)
    if not rel:
        return finish("failed.json", -1, "release with inputs not found")
    work = tempfile.mkdtemp(prefix=f"rh-{job_id}-")
    ind, outd = os.path.join(work, "in"), os.path.join(work, "out")
    os.makedirs(ind); os.makedirs(outd)
    try:
        total = 0
        assets = {a["name"]: a for a in rel.get("assets", [])}
        for n in job["inputs"]:
            if n not in assets:
                return finish("failed.json", -1, f"missing input {n}")
            total += assets[n].get("size", 0)
            if total > MAX_INPUT_TOTAL:
                return finish("failed.json", -1, "inputs over size cap")
            with open(os.path.join(ind, n), "wb") as f:
                f.write(gh.download_asset(assets[n]))
        binpath = os.path.join(ffmpeg_dir, job["cmd"][0])
        args = prefer_videotoolbox(rewrite_args(job["cmd"], ind, outd, binpath), vt)
        if job["cmd"][0] == "ffmpeg":
            args.insert(1, "-nostdin")
        log(f"running {job_id}: {args}")
        try:
            p = subprocess.run(args, cwd=work, capture_output=True, text=True, timeout=TIMEOUT, shell=False)
            code, tail = p.returncode, (p.stderr or "") + (p.stdout or "")
        except subprocess.TimeoutExpired:
            code, tail = -9, "timed out"
        if code == 0:
            for n in job["outputs"]:
                fp = os.path.join(outd, n)
                if not os.path.isfile(fp):
                    code, tail = -2, tail + f"\nmissing output {n}"; break
                if os.path.getsize(fp) > MAX_OUTPUT_EACH:
                    code, tail = -3, tail + f"\noutput {n} over size cap"; break
                with open(fp, "rb") as f:
                    gh.upload_asset(rel, n, f.read())
        for n in job["inputs"]:
            if n in assets:
                gh.delete_asset(assets[n])
        finish("done.json" if code == 0 else "failed.json", code, tail)
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
    ap.add_argument("--no-videotoolbox", action="store_true")
    ap.add_argument("--once", action="store_true", help="check once and exit (test)")
    ap.add_argument("--selftest", action="store_true", help="tiny local test render, prints PASS/FAIL")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest(a.ffmpeg_dir, not a.no_videotoolbox)
    gh = GitHub(keychain("render-helper-github"), a.repo)
    key = keychain("render-helper-hmac").encode()
    worker = socket.gethostname()
    print(f"render-helper worker on {worker}, polling {a.repo}", flush=True)
    while True:
        try:
            for jid in pending_jobs(gh):
                if try_claim(gh, jid, worker):
                    run_job(gh, jid, key, a.ffmpeg_dir, not a.no_videotoolbox,
                            log=lambda m: print(m, flush=True))
        except Exception as e:  # keep the agent alive through network blips
            print(f"error: {e}", file=sys.stderr, flush=True)
        if a.once:
            return 0
        time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
