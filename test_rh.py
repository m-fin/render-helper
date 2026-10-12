import io, json, unittest, urllib.error
from unittest import mock
import rh_common as c, rh_worker as w, rh_submit as s, rh_status as st, rh_dashboard as dash
import base64, os, tempfile

KEY = b"k" * 32
def job(**kw):
    j = s.build_job("bot", ["ffmpeg", "-i", "{in:a.mp4}", "-c:v", "libx264", "{out:b.mp4}"],
                    ["a.mp4"], ["b.mp4"], job_id="t1", created="2026-10-12T00:00:00Z")
    j.update(kw); return j

class Signing(unittest.TestCase):
    def test_roundtrip(self):
        self.assertTrue(c.verify_job(c.sign_job(job(), KEY), KEY))
    def test_tamper(self):
        j = c.sign_job(job(), KEY); j["cmd"] = ["ffmpeg", "-version"]
        self.assertFalse(c.verify_job(j, KEY))
    def test_wrong_key_and_missing(self):
        self.assertFalse(c.verify_job(c.sign_job(job(), KEY), b"x"))
        self.assertFalse(c.verify_job(job(), KEY))

class Validation(unittest.TestCase):
    def test_ok(self): self.assertTrue(c.validate_job(job()))
    def bad(self, cmd, **kw):
        with self.assertRaises(c.ValidationError): c.validate_job(job(cmd=cmd, **kw))
    def test_binary(self):
        self.bad(["sh", "-c", "x"]); self.bad(["/usr/bin/ffmpeg"]); self.bad(["bash"])
    def test_paths(self):
        self.bad(["ffmpeg", "-i", "/etc/passwd"]); self.bad(["ffmpeg", "-i", "../x"])
        self.bad(["ffmpeg", "-i", "~/x"]); self.bad(["ffmpeg", "-i", "file:/etc/passwd"])
    def test_protocols(self):
        for a in ["http://x/y", "https://x", "tcp://1.2.3.4:1", "concat:a|b", "pipe:1", "rtmp://x"]:
            self.bad(["ffmpeg", "-i", a])
        self.bad(["ffmpeg", "-vf", "movie=x.png"]); self.bad(["ffmpeg", "-vf", "sendcmd=f=c.txt"])
    def test_placeholder_and_names(self):
        self.bad(["ffmpeg", "-i", "{in:nope.mp4}"])
        self.bad(["ffmpeg", "-i", "{in:a.mp4}"], inputs=["../a.mp4"])
        self.bad(["ffmpeg"], id="../x")
    def test_lavfi_allowed(self):
        c.validate_job(job(cmd=["ffmpeg", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64", "{out:b.mp4}"]))

class Rewrite(unittest.TestCase):
    def test_rewrite(self):
        r = c.rewrite_args(job()["cmd"], "/t/in", "/t/out", "/opt/homebrew/bin/ffmpeg")
        self.assertEqual(r, ["/opt/homebrew/bin/ffmpeg", "-i", "/t/in/a.mp4", "-c:v", "libx264", "/t/out/b.mp4"])
    def test_vt(self):
        self.assertIn("h264_videotoolbox", c.prefer_videotoolbox(["-c:v", "libx264"]))
        self.assertIn("libx264", c.prefer_videotoolbox(["-c:v", "libx264"], False))

def resp(obj):
    m = mock.MagicMock(); m.__enter__.return_value.read.return_value = json.dumps(obj).encode(); return m

class Claim(unittest.TestCase):
    def http_err(self, code):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))
    def test_claim_success(self):
        calls = []
        def opener(r):
            calls.append(r.get_method())
            if r.get_method() == "GET": raise self.http_err(404)
            return resp({"content": {}})
        self.assertTrue(w.try_claim(c.GitHub("t", opener=opener), "t1", "mac"))
        self.assertEqual(calls, ["GET", "PUT"])
    def test_claim_race_conflict(self):
        def opener(r):
            if r.get_method() == "GET": raise self.http_err(404)
            raise self.http_err(422)
        self.assertFalse(w.try_claim(c.GitHub("t", opener=opener), "t1", "mac"))
    def test_claim_already_claimed(self):
        import base64
        body = {"content": base64.b64encode(b'{"worker":"x"}').decode(), "sha": "abc"}
        opener = mock.Mock(return_value=resp(body))
        self.assertFalse(w.try_claim(c.GitHub("t", opener=opener), "t1", "mac"))
        self.assertEqual(opener.call_count, 1)
    def test_put_without_sha_is_create_only(self):
        seen = {}
        def opener(r):
            seen.update(json.loads(r.data)); return resp({})
        c.GitHub("t", opener=opener).put_file("jobs/x/claimed.json", {}, "m")
        self.assertNotIn("sha", seen)

class WorkerRefusal(unittest.TestCase):
    def test_unsigned_job_refused_without_running(self):
        gh = mock.Mock(); gh.get_file.return_value = (job(), "sha")
        with mock.patch("subprocess.run") as run:
            w.run_job(gh, "t1", KEY, "/opt/homebrew/bin")
            run.assert_not_called()
        self.assertEqual(gh.put_file.call_args[0][0], "jobs/t1/failed.json")
    def test_bad_args_signed_refused(self):
        gh = mock.Mock(); gh.get_file.return_value = (c.sign_job(job(cmd=["ffmpeg", "-i", "/etc/passwd"]), KEY), "s")
        with mock.patch("subprocess.run") as run:
            w.run_job(gh, "t1", KEY, "/x"); run.assert_not_called()


class SelfTest(unittest.TestCase):
    def test_selftest_mocked(self):
        def fake(args, **kw):
            open(args[-1], "wb").write(b"x"); return mock.Mock(returncode=0, stderr="")
        with mock.patch("subprocess.run", side_effect=fake) as run:
            self.assertEqual(w.selftest("/opt/homebrew/bin"), 0)
            self.assertIn("h264_videotoolbox", run.call_args[0][0])


class FakeGH(c.GitHub):
    """In-memory GitHub: contents API + releases/assets. Counts commits."""
    def __init__(self):
        self.repo = "m-fin/render-helper"
        self.files, self.releases, self.commits, self.next_id = {}, {}, 0, 1
    def req(self, method, url, body=None, raw=None, ctype=None, accept=None):
        if url.startswith("/contents/"):
            path = url[len("/contents/"):]
            if method == "GET":
                if path in self.files:
                    return {"content": base64.b64encode(self.files[path][0]).decode(), "sha": str(self.files[path][1])}
                kids = sorted({k[len(path)+1:].split("/")[0] for k in self.files if k.startswith(path + "/")})
                if not kids: return None
                return [{"name": n, "type": "dir" if any(k.startswith(f"{path}/{n}/") for k in self.files) else "file"} for n in kids]
            if method == "PUT":
                cur = self.files.get(path)
                if (cur and body.get("sha") != str(cur[1])) or (not cur and "sha" in body):
                    raise c.Conflict("sha")
                self.files[path] = (base64.b64decode(body["content"]), (cur[1] + 1) if cur else 1)
                self.commits += 1; return {}
        if url.startswith("/releases/tags/"):
            return self.releases.get(url.split("/")[-1])
        if url.startswith("/releases?"):
            return list(self.releases.values())
        if url == "/releases" and method == "POST":
            r = {"id": self.next_id, "tag_name": body["tag_name"], "assets": []}; self.next_id += 1
            self.releases[body["tag_name"]] = r; return r
        if "/assets?name=" in url:
            name = url.split("name=")[1]
            rel = next(r for r in self.releases.values() if f"/releases/{r['id']}/" in url)
            a = {"id": self.next_id, "name": name, "size": len(raw), "data": raw}; self.next_id += 1
            rel["assets"].append(a); return a
        if url.startswith("/releases/assets/"):
            aid = int(url.split("/")[-1])
            for r in self.releases.values():
                for a in r["assets"]:
                    if a["id"] == aid:
                        if method == "DELETE": r["assets"].remove(a); return None
                        return a["data"]
        raise AssertionError(f"unexpected {method} {url}")

INFO = {"model": "Mac14,2", "macos": "15.1", "chip": "Apple M2", "arch": "arm64", "ffmpeg": "ffmpeg version 7.1"}

class Handshake(unittest.TestCase):
    def test_hello_once(self):
        gh = FakeGH()
        self.assertTrue(w.ensure_hello(gh, "mac-1", KEY, INFO))
        self.assertFalse(w.ensure_hello(gh, "mac-1", KEY, INFO))
        self.assertEqual(gh.commits, 1)
        hello = json.loads(gh.files["workers/mac-1/hello.json"][0])
        self.assertTrue(c.verify_obj(hello, KEY)); self.assertEqual(hello["model"], "Mac14,2")
    def test_heartbeat_replaces_asset_no_commit(self):
        gh = FakeGH()
        for _ in range(3): w.send_heartbeat(gh, "mac-1", KEY, INFO)
        self.assertEqual(gh.commits, 0)
        names = [a["name"] for a in gh.releases["worker-status"]["assets"]]
        self.assertEqual(names, ["heartbeat-mac-1.json"])
    def test_status_online_offline(self):
        gh = FakeGH(); w.ensure_hello(gh, "mac-1", KEY, INFO); hb = w.send_heartbeat(gh, "mac-1", KEY, INFO)
        t = c.parse_iso(hb["at"])
        r = st.worker_status(gh, KEY, now=t + 30)[0]
        self.assertTrue(r["online"]); self.assertEqual(r["ffmpeg"], "ffmpeg version 7.1")
        self.assertFalse(st.worker_status(gh, KEY, now=t + 601)[0]["online"])
        self.assertIn("ONLINE", st.fmt([r]))
    def test_status_rejects_forged(self):
        gh = FakeGH(); w.ensure_hello(gh, "mac-1", KEY, INFO); w.send_heartbeat(gh, "mac-1", b"wrong", INFO)
        r = st.worker_status(gh, KEY)[0]
        self.assertFalse(r["online"]); self.assertIn("signature", r["problem"])
        gh2 = FakeGH(); w.ensure_hello(gh2, "mac-1", b"wrong", INFO)
        self.assertFalse(st.worker_status(gh2, KEY)[0]["paired"])
    def test_no_workers(self):
        self.assertEqual(st.worker_status(FakeGH(), KEY), [])
    def test_test_job_requires_online(self):
        with mock.patch.dict(os.environ, {"RH_GITHUB_TOKEN": "t", "RH_HMAC_KEY": "k"}), \
             mock.patch.object(st, "GitHub", return_value=FakeGH()), \
             mock.patch("rh_submit.submit") as sub:
            self.assertEqual(st.main(["--test-job"]), 1); sub.assert_not_called()
    def test_worker_id_stable(self):
        d = tempfile.mkdtemp(); self.assertEqual(w.worker_id(d), w.worker_id(d))
    def test_mac_info_mocked(self):
        with mock.patch.object(w, "_out", side_effect=lambda a: {"hw.model": "Mac14,2"}.get(a[-1], "ffmpeg version 7.1\nx")):
            self.assertEqual(w.mac_info("/x")["model"], "Mac14,2")
            self.assertEqual(w.mac_info("/x")["ffmpeg"], "ffmpeg version 7.1")

class Stats(unittest.TestCase):
    def test_estimate_labeled(self):
        e = c.estimate_cloud_cpu_minutes(120.0, 30.0)
        self.assertEqual(e["value"], 2.0); self.assertTrue(e["is_estimate"]); self.assertIn("cpu_s", e["formula"])
        self.assertEqual(c.estimate_cloud_cpu_minutes(None, 60.0)["basis"], "wall_s")
    def test_job_writes_stats(self):
        gh = FakeGH()
        j = c.sign_job(job(), KEY); gh.files["jobs/t1/job.json"] = (json.dumps(j).encode(), 1)
        rel = gh.create_release("job-t1", "job-t1"); gh.upload_asset(rel, "a.mp4", b"12345")
        def fake(args, **kw):
            open(args[-1], "wb").write(b"x" * 7); return mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch("subprocess.run", side_effect=fake):
            w.run_job(gh, "t1", KEY, "/opt/homebrew/bin", worker="mac-1", log=lambda m: None)
        recs = dash.load_jsonl(gh.files["stats/jobs.jsonl"][0].decode())
        self.assertEqual(len(recs), 1); r = recs[0]
        self.assertEqual((r["input_bytes"], r["output_bytes"], r["exit_code"], r["worker"]), (5, 7, 0, "mac-1"))
        self.assertTrue(r["cloud_cpu_minutes_saved_estimate"]["is_estimate"])
        self.assertIn("stats", json.loads(gh.files["jobs/t1/done.json"][0]))
    def test_append_keeps_old_lines(self):
        gh = FakeGH(); w.append_stats(gh, {"job_id": "a"}); w.append_stats(gh, {"job_id": "b"})
        self.assertEqual([r["job_id"] for r in dash.load_jsonl(gh.files["stats/jobs.jsonl"][0].decode())], ["a", "b"])
    def test_dashboard(self):
        est = c.estimate_cloud_cpu_minutes(60.0, 10.0)
        recs = [{"job_id": "a", "exit_code": 0, "wall_s": 10, "cpu_s": 60.0, "input_bytes": 1, "output_bytes": 2,
                 "cloud_cpu_minutes_saved_estimate": est},
                {"job_id": "<b>", "exit_code": 1, "wall_s": 1, "cpu_s": None, "input_bytes": 0, "output_bytes": 0,
                 "cloud_cpu_minutes_saved_estimate": c.estimate_cloud_cpu_minutes(None, 1)}]
        s = dash.summarize(recs); self.assertEqual((s["jobs"], s["ok"], s["est_cloud_cpu_min"]), (2, 1, 1.0))
        h = dash.render_html(recs, s); self.assertIn("Estimated", h); self.assertIn("&lt;b&gt;", h)
        self.assertIn("ESTIMATED", dash.text_summary(s))

import tarfile, io, hashlib
def _tar(members):
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for name, data, kind in members:
            ti = tarfile.TarInfo(name); ti.size = len(data)
            if kind == "sym": ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"; ti.size = 0
            t.addfile(ti, io.BytesIO(data) if kind != "sym" else None)
    return b.getvalue()

def cjob(**over):
    capd = {"bundle": "g.tar", "bundle_sha256": "0" * 64, "seed": 1, "w": 640, "h": 360, "limit": 30, "out": "v.mp4"}
    capd.update(over)
    return {"id": "c1", "bot": "t", "created": "x", "cmd": ["capture"], "inputs": ["g.tar"], "outputs": ["v.mp4"], "capture": capd}

class Capture(unittest.TestCase):
    def test_valid(self): self.assertTrue(c.validate_job(cjob()))
    def test_rejects(self):
        for bad in (dict(seed="1"), dict(w=99999), dict(out="v.mov"), dict(bundle="x.tar"), dict(bundle_sha256="zz")):
            with self.assertRaises(c.ValidationError): c.validate_job(cjob(**bad))
        j = cjob(); j["capture"]["url"] = "http://x"
        with self.assertRaises(c.ValidationError): c.validate_job(j)
        j = cjob(); j["cmd"] = ["capture", "--evil"]
        with self.assertRaises(c.ValidationError): c.validate_job(j)
        j = job(); j["capture"] = cjob()["capture"]
        with self.assertRaises(c.ValidationError): c.validate_job(j)
    def test_safe_extract(self):
        d = tempfile.mkdtemp()
        for m in ([("../x", b"1", "f")], [("/abs", b"1", "f")], [("l", b"", "sym")]):
            p = os.path.join(d, "t.tar"); open(p, "wb").write(_tar(m))
            with self.assertRaises(c.ValidationError): c.safe_extract(p, os.path.join(d, "o"))
        open(p, "wb").write(_tar([("a/b.mjs", b"ok", "f")])); c.safe_extract(p, os.path.join(d, "o"))
        self.assertEqual(open(os.path.join(d, "o/a/b.mjs")).read(), "ok")
    def _run(self, data, sha):
        gh = FakeGH(); j = c.sign_job(cjob(bundle_sha256=sha), KEY)
        gh.files["jobs/c1/job.json"] = (json.dumps(j).encode(), 1)
        rel = gh.create_release("job-c1", "job-c1"); gh.upload_asset(rel, "g.tar", data)
        calls = []
        def fake(args, **kw):
            calls.append((args, kw)); open(args[6], "wb").write(b"v"); return mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch("subprocess.run", side_effect=fake):
            w.run_job(gh, "c1", KEY, "/ff", worker="m", log=lambda m: None, node_dir="/nd")
        return gh, calls
    def test_fixed_script_and_hash(self):
        data = _tar([("capture.html", b"x", "f")])
        gh, calls = self._run(data, hashlib.sha256(data).hexdigest())
        args, kw = calls[0]
        self.assertEqual(args[0], "/nd/node"); self.assertEqual(args[1], w.CAPTURE_SCRIPT)
        self.assertEqual(args[-1], "/ff/ffmpeg"); self.assertNotIn("RH_GITHUB_TOKEN", kw["env"])
        self.assertIn("jobs/c1/done.json", gh.files)
    def test_hash_mismatch_refused(self):
        gh, calls = self._run(_tar([("capture.html", b"x", "f")]), "1" * 64)
        self.assertEqual(calls, []); self.assertIn("jobs/c1/failed.json", gh.files)
    def test_exact_encoder_keeps_x264(self):
        gh = FakeGH(); j = job(); j["exact_encoder"] = True; j = c.sign_job(j, KEY)
        gh.files["jobs/t1/job.json"] = (json.dumps(j).encode(), 1)
        rel = gh.create_release("job-t1", "job-t1"); gh.upload_asset(rel, "a.mp4", b"1")
        seen = []
        def fake(args, **kw):
            seen.append(args); open(args[-1], "wb").write(b"x"); return mock.Mock(returncode=0, stderr="", stdout="")
        with mock.patch("subprocess.run", side_effect=fake):
            w.run_job(gh, "t1", KEY, "/ff", worker="m", log=lambda m: None)
        self.assertNotIn("h264_videotoolbox", seen[0])

if __name__ == "__main__":
    unittest.main()
