#!/usr/bin/env python3
"""Bot side: queue an ffmpeg job for the Mac worker and fetch the results.

Env: RH_GITHUB_TOKEN (fine-grained PAT, Contents R/W on m-fin/render-helper), RH_HMAC_KEY.
Example:
  rh_submit.py --bot video-creator -i frames.zip -o out.mp4 -- \
      ffmpeg -i {in:frames.zip} -c:v libx264 {out:out.mp4}
"""
import argparse, os, sys, time, uuid, datetime
from rh_common import GitHub, sign_job, validate_job, check_name, REPO

MAX_INPUT_BYTES = 1900 * 1024 * 1024


def build_job(bot, cmd, inputs, outputs, job_id=None, created=None, exact_encoder=False, capture=None):
    j = _base(bot, cmd, inputs, outputs, job_id, created)
    if exact_encoder:
        j["exact_encoder"] = True
    if capture is not None:
        j["capture"] = capture
    return j


def _base(bot, cmd, inputs, outputs, job_id, created):
    return {
        "id": job_id or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8],
        "bot": bot,
        "created": created or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat() + "Z",
        "cmd": list(cmd),
        "inputs": [os.path.basename(p) for p in inputs],
        "outputs": list(outputs),
    }


def submit(gh, job, key, input_paths, outdir=".", poll=15, timeout=3 * 3600):
    validate_job(job)
    job = sign_job(job, key)
    tag = f"job-{job['id']}"
    rel = gh.create_release(tag, tag)
    for p in input_paths:
        if os.path.getsize(p) > MAX_INPUT_BYTES:
            raise SystemExit(f"{p} too big for a release asset")
        with open(p, "rb") as f:
            gh.upload_asset(rel, check_name(os.path.basename(p)), f.read())
    # job.json last, so the worker never sees a job whose inputs aren't up yet
    gh.put_file(f"jobs/{job['id']}/job.json", job, f"queue {job['id']}")
    print(f"queued {job['id']}", file=sys.stderr)
    deadline = time.time() + timeout
    while time.time() < deadline:
        done, _ = gh.get_file(f"jobs/{job['id']}/done.json")
        failed, _ = gh.get_file(f"jobs/{job['id']}/failed.json")
        if done or failed:
            break
        time.sleep(poll)
    else:
        raise SystemExit("timed out waiting for worker (job left queued)")
    rel = gh.find_release(tag)
    if done:
        os.makedirs(outdir, exist_ok=True)
        for a in rel.get("assets", []):
            if a["name"] in job["outputs"]:
                with open(os.path.join(outdir, a["name"]), "wb") as f:
                    f.write(gh.download_asset(a))
    gh.delete_release(rel, tag)
    status = done or failed
    print(f"{'done' if done else 'FAILED'} exit={status.get('exit_code')}\n{status.get('log_tail','')}", file=sys.stderr)
    return 0 if done else 1


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("-i", "--input", action="append", default=[])
    ap.add_argument("-o", "--output", action="append", default=[])
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--exact-encoder", action="store_true", help="keep libx264 (no VideoToolbox swap)")
    ap.add_argument("--capture", help="browser capture: game .tar bundle (runs the repo's fixed rh_capture.mjs)")
    ap.add_argument("--bench", action="store_true", help="capture-bench: lossless capture + 3 encodes on the Mac")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--size", default="1920x1080")
    ap.add_argument("--limit", type=int, default=0, help="frames (0 = whole run)")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    token, key = os.environ.get("RH_GITHUB_TOKEN"), os.environ.get("RH_HMAC_KEY")
    if not token or not key:
        raise SystemExit("set RH_GITHUB_TOKEN and RH_HMAC_KEY")
    cap = None
    if a.capture:
        import hashlib
        w, h = (int(x) for x in a.size.split("x"))
        cap = {"bundle": os.path.basename(a.capture), "seed": a.seed, "w": w, "h": h, "limit": a.limit,
               "out": (a.output or ["x.mp4"])[0], "bundle_sha256": hashlib.sha256(open(a.capture, "rb").read()).hexdigest()}
        if a.bench:
            from rh_common import BENCH_OUTPUTS
            a.output = list(BENCH_OUTPUTS); cap["out"] = BENCH_OUTPUTS[0]; cap["bench"] = 1
        cmd, a.input = ["capture"], [a.capture] + a.input
    job = build_job(a.bot, cmd, a.input, a.output, exact_encoder=a.exact_encoder, capture=cap)
    return submit(GitHub(token, a.repo), job, key.encode(), a.input, a.outdir)


if __name__ == "__main__":
    sys.exit(main())
