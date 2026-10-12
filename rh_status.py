#!/usr/bin/env python3
"""Bot side: is the Mac worker online? Verifies signed hello.json + heartbeat assets.

  rh_status.py              # print online/offline per paired worker
  rh_status.py --json       # machine-readable
  rh_status.py --test-job   # if a worker is online, send the tiny 1 s 64x64 test job
Env: RH_GITHUB_TOKEN, RH_HMAC_KEY.  Offline = no valid heartbeat for 10 minutes.
"""
import argparse, json, os, sys, time
from rh_common import GitHub, verify_obj, parse_iso, REPO, STATUS_TAG, OFFLINE_AFTER

TEST_CMD = ["ffmpeg", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64", "-c:v", "libx264", "{out:t.mp4}"]


def worker_status(gh, key, now=None, offline_after=OFFLINE_AFTER):
    now = now if now is not None else time.time()
    rel = gh.find_release(STATUS_TAG) or {}
    beats = {a["name"]: a for a in rel.get("assets", [])}
    out = []
    for d in gh.list_dir("workers"):
        if d.get("type") != "dir":
            continue
        wid = d["name"]
        hello, _ = gh.get_file(f"workers/{wid}/hello.json")
        row = {"worker_id": wid, "paired": False, "online": False, "last_seen": None,
               "model": None, "macos": None, "chip": None, "ffmpeg": None, "state": None, "problem": None}
        if not hello or not verify_obj(hello, key) or hello.get("worker_id") != wid:
            row["problem"] = "hello.json missing or bad signature"
            out.append(row); continue
        row.update(paired=True, model=hello.get("model"), macos=hello.get("macos"),
                   chip=hello.get("chip"), ffmpeg=hello.get("ffmpeg"))
        a = beats.get(f"heartbeat-{wid}.json")
        if not a:
            row["problem"] = "no heartbeat yet"
        else:
            try:
                hb = json.loads(gh.download_asset(a))
            except Exception:
                hb = None
            if not hb or not verify_obj(hb, key) or hb.get("worker_id") != wid:
                row["problem"] = "heartbeat bad signature"
            else:
                age = now - parse_iso(hb["at"])
                row.update(last_seen=hb["at"], age_s=int(age), state=hb.get("state"),
                           online=age <= offline_after, ffmpeg=hb.get("ffmpeg") or row["ffmpeg"])
        out.append(row)
    return out


def fmt(rows):
    if not rows:
        return "no workers paired yet (waiting for hello.json from the Mac)"
    lines = []
    for r in rows:
        tag = "ONLINE" if r["online"] else "OFFLINE"
        extra = f" last seen {r['last_seen']} ({r.get('age_s')} s ago), {r['state']}" if r["last_seen"] else ""
        prob = f"  [{r['problem']}]" if r["problem"] else ""
        lines.append(f"{tag}  {r['worker_id']}  {r['model']}  macOS {r['macos']}  {r['chip']}  {r['ffmpeg']}{extra}{prob}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--test-job", action="store_true")
    a = ap.parse_args(argv)
    token, key = os.environ.get("RH_GITHUB_TOKEN"), os.environ.get("RH_HMAC_KEY")
    if not token or not key:
        raise SystemExit("set RH_GITHUB_TOKEN and RH_HMAC_KEY")
    gh, key = GitHub(token, a.repo), key.encode()
    rows = worker_status(gh, key)
    print(json.dumps(rows, indent=1) if a.json else fmt(rows))
    if a.test_job:
        if not any(r["online"] for r in rows):
            print("no worker online; test job not sent", file=sys.stderr); return 1
        import rh_submit
        return rh_submit.submit(gh, rh_submit.build_job("rh-status-test", TEST_CMD, [], ["t.mp4"]), key, [])
    return 0 if any(r["online"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
