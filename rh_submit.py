#!/usr/bin/env python3
"""Bot side: queue an ffmpeg job for the Mac worker and fetch the results.

Env: RH_GITHUB_TOKEN (fine-grained PAT, Contents R/W on m-fin/render-helper), RH_HMAC_KEY.
Example:
  rh_submit.py --bot video-creator -i frames.zip -o out.mp4 -- \
      ffmpeg -i {in:frames.zip} -c:v libx264 {out:out.mp4}
"""
import argparse, os, sys, time, uuid, datetime
from rh_common import Conflict, GitHub, sign_job, verify_obj, validate_job, check_name, REPO

MAX_INPUT_BYTES = 1900 * 1024 * 1024


def build_job(bot, cmd, inputs, outputs, job_id=None, created=None, exact_encoder=False, capture=None, post=None):
    j = _base(bot, cmd, inputs, outputs, job_id, created)
    if post:
        j["post"] = post
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


def submit(gh, job, key, input_paths, outdir=".", poll=15, timeout=3 * 3600, xfer=None):
    """xfer: an rh_xfer.XferClient -> inputs/outputs go over Tailscale (queue stays on GitHub,
    no release). Falls back to the GitHub release path if the endpoint doesn't answer."""
    if xfer is not None and not xfer.ping():
        print("tailscale endpoint unreachable; falling back to github transport", file=sys.stderr)
        xfer = None
    if xfer is not None:
        return submit_tailscale(gh, job, key, input_paths, xfer, outdir, poll, timeout)
    validate_job(job)
    job = sign_job(job, key)
    tag = f"job-{job['id']}"
    rel = gh.create_release(tag, tag)
    for p in input_paths:
        if os.path.getsize(p) > MAX_INPUT_BYTES:
            raise SystemExit(f"{p} too big for a release asset")
        with open(p, "rb") as f:
            gh.upload_asset(rel, check_name(os.path.basename(p)), f.read())
    _queue(gh, job, lambda: gh.delete_release(rel, tag))
    done, failed = _wait(gh, job, poll, timeout)
    rel = gh.find_release(tag)
    if done:
        os.makedirs(outdir, exist_ok=True)
        for a in rel.get("assets", []):
            if a["name"] in job["outputs"]:
                with open(os.path.join(outdir, a["name"]), "wb") as f:
                    f.write(gh.download_asset(a))
    gh.delete_release(rel, tag)
    return _report(done, failed)


def submit_tailscale(gh, job, key, input_paths, xfer, outdir=".", poll=15, timeout=3 * 3600):
    from rh_xfer import file_sha256
    for p in input_paths:
        if os.path.getsize(p) > MAX_INPUT_BYTES:
            raise SystemExit(f"{p} too big")
    job = dict(job, transport="tailscale",
               input_sha256={check_name(os.path.basename(p)): file_sha256(p) for p in input_paths})
    validate_job(job)
    job = sign_job(job, key)
    for p in input_paths:
        xfer.put(job["id"], p)  # server checks sha256; worker re-checks against the signed job
    _queue(gh, job, lambda: xfer.delete(job["id"]))
    done, failed = _wait(gh, job, poll, timeout)
    try:
        if done:
            if not verify_obj(done, key) or not isinstance(done.get("outputs_sha256"), dict):
                raise SystemExit("done.json not signed by the worker; not trusting outputs")
            os.makedirs(outdir, exist_ok=True)
            for n in job["outputs"]:
                got = xfer.get(job["id"], n, os.path.join(outdir, n))
                if got != done["outputs_sha256"].get(n):
                    os.remove(os.path.join(outdir, n))
                    raise SystemExit(f"{n}: sha256 does not match signed done.json")
    finally:
        try:
            xfer.delete(job["id"])
        except Exception as e:
            print(f"cleanup on Mac failed (purged after 24h): {e}", file=sys.stderr)
    return _report(done, failed)


def _queue(gh, job, cleanup):
    # job.json last, so the worker never sees a job whose inputs aren't up yet
    for attempt in range(5):  # other bots commit to main too; a 409 here is a branch-head race
        try:
            gh.put_file(f"jobs/{job['id']}/job.json", job, f"queue {job['id']}")
            break
        except Conflict:
            if attempt == 4:
                cleanup()  # don't leave orphan (public) input assets behind
                raise
            time.sleep(2 + attempt * 2)
    print(f"queued {job['id']}", file=sys.stderr)


def _wait(gh, job, poll, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        done, _ = gh.get_file(f"jobs/{job['id']}/done.json")
        failed, _ = gh.get_file(f"jobs/{job['id']}/failed.json")
        if done or failed:
            break
        time.sleep(poll)
    else:
        raise SystemExit("timed out waiting for worker (job left queued)")
    return done, failed


def _report(done, failed):
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
    ap.add_argument("--transport", choices=("github", "tailscale"), default="github",
                    help="tailscale: send files over the Mac's signed Tailscale endpoint (queue stays on GitHub)")
    ap.add_argument("--xfer-host", default=os.environ.get("RH_XFER_HOST", "100.78.67.27"))
    ap.add_argument("--audio", help="mux this audio file into the first video output on the Mac")
    ap.add_argument("--lufs", type=float, help="loudness-normalise output audio (e.g. -14)")
    ap.add_argument("--resume", metavar="JOB_ID", help="re-attach to a queued job and wait/report status")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    token, key = os.environ.get("RH_GITHUB_TOKEN"), os.environ.get("RH_HMAC_KEY")
    if not token or not key:
        raise SystemExit("set RH_GITHUB_TOKEN and RH_HMAC_KEY")
    if a.resume:
        gh = GitHub(token, a.repo)
        check_name(a.resume)
        j, _ = gh.get_file(f"jobs/{a.resume}/job.json")
        if not j:
            raise SystemExit("no such job")
        claimed, _ = gh.get_file(f"jobs/{a.resume}/claimed.json")
        print(f"{a.resume}: {'claimed by ' + str(claimed.get('worker')) if claimed else 'queued, waiting for Mac'}", file=sys.stderr)
        return _report(*_wait(gh, j, 15, 3 * 3600))
    post = {k: v for k, v in (("audio", os.path.basename(a.audio) if a.audio else None), ("lufs", a.lufs)) if v is not None} or None
    if a.audio:
        a.input.append(a.audio)
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
    job = build_job(a.bot, cmd, a.input, a.output, exact_encoder=a.exact_encoder, capture=cap, post=post)
    xfer = None
    if a.transport == "tailscale":
        from rh_xfer import XferClient
        xfer = XferClient(key.encode(), a.xfer_host)
    return submit(GitHub(token, a.repo), job, key.encode(), a.input, a.outdir, xfer=xfer)


if __name__ == "__main__":
    sys.exit(main())
