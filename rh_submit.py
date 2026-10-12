#!/usr/bin/env python3
"""Bot side: queue an ffmpeg job for the Mac worker and fetch the results.

Env: RH_GITHUB_TOKEN (fine-grained PAT, Contents R/W on m-fin/homerender), RH_HMAC_KEY.
Example:
  rh_submit.py --bot video-creator -i frames.zip -o out.mp4 -- \
      ffmpeg -i {in:frames.zip} -c:v libx264 {out:out.mp4}
"""
import argparse, os, sys, time, uuid, datetime
from rh_common import GitHub, sign_job, validate_job, check_name, REPO

MAX_INPUT_BYTES = 1900 * 1024 * 1024


def build_job(bot, cmd, inputs, outputs, job_id=None, created=None):
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
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    token, key = os.environ.get("RH_GITHUB_TOKEN"), os.environ.get("RH_HMAC_KEY")
    if not token or not key:
        raise SystemExit("set RH_GITHUB_TOKEN and RH_HMAC_KEY")
    job = build_job(a.bot, cmd, a.input, a.output)
    return submit(GitHub(token, a.repo), job, key.encode(), a.input, a.outdir)


if __name__ == "__main__":
    sys.exit(main())
