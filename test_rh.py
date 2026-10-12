import io, json, unittest, urllib.error
from unittest import mock
import rh_common as c, rh_worker as w, rh_submit as s

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

if __name__ == "__main__":
    unittest.main()
