#!/usr/bin/env python3
"""Build a static status.html + text summary from stats/jobs.jsonl (and worker status).

  rh_dashboard.py --jsonl stats/jobs.jsonl -o status.html     # from a local file, offline
  rh_dashboard.py -o status.html                              # fetch from the repo (needs env)
All timings/sizes are what the Mac measured. "Cloud CPU-minutes saved" is an ESTIMATE.
"""
import argparse, base64, html, json, os, sys
from rh_common import GitHub, REPO, CLOUD_CPU_FACTOR


def load_jsonl(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def summarize(recs):
    ok = [r for r in recs if r.get("exit_code") == 0]
    return {"jobs": len(recs), "ok": len(ok), "failed": len(recs) - len(ok),
            "wall_s": round(sum(r.get("wall_s") or 0 for r in recs), 1),
            "cpu_s": round(sum(r.get("cpu_s") or 0 for r in recs), 1),
            "cpu_measured_jobs": sum(1 for r in recs if r.get("cpu_s") is not None),
            "input_bytes": sum(r.get("input_bytes") or 0 for r in recs),
            "output_bytes": sum(r.get("output_bytes") or 0 for r in recs),
            "est_cloud_cpu_min": round(sum((r.get("cloud_cpu_minutes_saved_estimate") or {}).get("value", 0) for r in ok), 2)}


def mb(n): return f"{n / 1e6:.1f} MB"


def text_summary(s):
    return (f"{s['jobs']} jobs ({s['ok']} ok, {s['failed']} failed); measured render time {s['wall_s']} s wall, "
            f"{s['cpu_s']} s CPU ({s['cpu_measured_jobs']} jobs with CPU time); in {mb(s['input_bytes'])}, "
            f"out {mb(s['output_bytes'])}; ESTIMATED cloud CPU-minutes saved: {s['est_cloud_cpu_min']} "
            f"(cpu_s, or wall_s if no CPU time, / 60 x {CLOUD_CPU_FACTOR}, successful jobs only)")


def render_html(recs, s, workers=None):
    e = html.escape
    wrows = "".join(f"<tr><td>{'🟢 online' if w['online'] else '🔴 offline'}</td><td>{e(str(w['worker_id']))}</td>"
                    f"<td>{e(str(w['model']))}</td><td>{e(str(w['ffmpeg']))}</td><td>{e(str(w['last_seen']))}</td></tr>"
                    for w in (workers or []))
    jrows = "".join(f"<tr><td>{e(str(r.get('finished')))}</td><td>{e(str(r.get('job_id')))}</td><td>{e(str(r.get('bot')))}</td>"
                    f"<td>{r.get('exit_code')}</td><td>{r.get('wall_s')}</td><td>{'' if r.get('cpu_s') is None else r['cpu_s']}</td>"
                    f"<td>{mb(r.get('input_bytes') or 0)}</td><td>{mb(r.get('output_bytes') or 0)}</td>"
                    f"<td>{(r.get('cloud_cpu_minutes_saved_estimate') or {}).get('value', '')}</td></tr>"
                    for r in reversed(recs[-200:]))
    return f"""<!doctype html><meta charset="utf-8"><title>Mac Render Helper status</title>
<style>body{{font:14px system-ui;margin:2em;max-width:1100px}}table{{border-collapse:collapse;width:100%;margin:1em 0}}
td,th{{border:1px solid #ccc;padding:4px 8px;text-align:left}}.est{{color:#a60}}</style>
<h1>Mac Render Helper</h1>
<h2>Workers</h2><table><tr><th>Status</th><th>Worker</th><th>Model</th><th>ffmpeg</th><th>Last heartbeat (UTC)</th></tr>{wrows or '<tr><td colspan=5>not checked</td></tr>'}</table>
<h2>Totals (measured on the Mac)</h2>
<p>{s['jobs']} jobs, {s['ok']} ok, {s['failed']} failed. Render time {s['wall_s']} s wall / {s['cpu_s']} s CPU. In {mb(s['input_bytes'])}, out {mb(s['output_bytes'])}.</p>
<p class="est"><b>Estimated</b> cloud CPU-minutes saved: <b>{s['est_cloud_cpu_min']}</b> (estimate, not measured).<br>
Formula per successful job: measured ffmpeg CPU seconds (user+sys) &divide; 60 &times; {CLOUD_CPU_FACTOR}; uses wall seconds when CPU time wasn't recorded.
The factor {CLOUD_CPU_FACTOR} is an assumption (cloud vCPU ≈ Mac CPU-second). VideoToolbox work runs on the Mac's media engine, so CPU time understates it.</p>
<h2>Jobs</h2><table><tr><th>Finished (UTC)</th><th>Job</th><th>Bot</th><th>Exit</th><th>Wall s</th><th>CPU s</th><th>In</th><th>Out</th><th class="est">Est. cloud CPU-min</th></tr>{jrows}</table>
"""


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", help="local stats file instead of fetching from the repo")
    ap.add_argument("-o", "--out", default="status.html")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args(argv)
    workers = None
    if a.jsonl:
        with open(a.jsonl) as f:
            recs = load_jsonl(f.read())
    else:
        token, key = os.environ.get("RH_GITHUB_TOKEN"), os.environ.get("RH_HMAC_KEY")
        if not token or not key:
            raise SystemExit("set RH_GITHUB_TOKEN and RH_HMAC_KEY, or pass --jsonl")
        gh = GitHub(token, a.repo)
        r = gh.req("GET", "/contents/stats/jobs.jsonl")
        recs = load_jsonl(base64.b64decode(r["content"]).decode()) if r else []
        import rh_status
        workers = rh_status.worker_status(gh, key.encode())
    s = summarize(recs)
    with open(a.out, "w") as f:
        f.write(render_html(recs, s, workers))
    print(text_summary(s)); print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
