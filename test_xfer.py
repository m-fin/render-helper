import hashlib, http.client, json, os, tempfile, threading, time, unittest, uuid
from unittest import mock
import rh_common as c, rh_xfer as x, rh_worker as w, rh_submit as s
from test_rh import FakeGH, KEY, job

def server(root, **kw):
    srv = x.XferServer(("127.0.0.1", 0), KEY, root, allow_any_addr=True, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.root = os.path.join(self.tmp, "xfer")
        self.srv = server(self.root)
        self.cl = x.XferClient(KEY, "127.0.0.1", self.srv.server_address[1], mode="direct")
    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close()
    def f(self, name, data):
        p = os.path.join(self.tmp, name); open(p, "wb").write(data); return p
    def raw(self, method, path, body=b"", ts=None, nonce=None, sha=None, key=KEY):
        ts = int(time.time()) if ts is None else ts; nonce = nonce or uuid.uuid4().hex
        sha = sha or hashlib.sha256(body).hexdigest()
        h = {"X-RH-TS": str(ts), "X-RH-Nonce": nonce, "X-RH-SHA256": sha,
             "X-RH-Sig": x.sign_req(key, method, path, ts, nonce, sha)}
        cn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        cn.request(method, path, body=body or None, headers=h); r = cn.getresponse(); r.read(); cn.close()
        return r.status

class Bind(unittest.TestCase):
    def test_refuses_non_tailscale(self):
        for ip in ("0.0.0.0", "192.168.1.5", "127.0.0.1", "100.128.0.1"):
            with self.assertRaises(RuntimeError): x.XferServer((ip, 0), KEY, tempfile.mkdtemp())
    def test_tailscale_ip_detection(self):
        ok = mock.Mock(returncode=0, stdout="100.78.67.27\n", stderr="")
        self.assertEqual(x.tailscale_ipv4(run=lambda *a, **k: ok), "100.78.67.27")
        bad = mock.Mock(returncode=0, stdout="192.168.1.9\n", stderr="")
        with self.assertRaises(RuntimeError): x.tailscale_ipv4(run=lambda *a, **k: bad)
        def missing(*a, **k): raise FileNotFoundError
        with self.assertRaises(RuntimeError): x.tailscale_ipv4(run=missing)

class Transfer(Base):
    def test_roundtrip_and_delete(self):
        self.assertTrue(self.cl.ping())
        sha = self.cl.put("j1", self.f("a.mp4", b"hello" * 1000))
        stored = os.path.join(self.root, "j1", "in", "a.mp4")
        self.assertEqual(open(stored, "rb").read(), b"hello" * 1000)
        os.makedirs(os.path.join(self.root, "j1", "out")); open(os.path.join(self.root, "j1", "out", "b.mp4"), "wb").write(b"out")
        dest = os.path.join(self.tmp, "b.mp4")
        self.assertEqual(self.cl.get("j1", "b.mp4", dest), hashlib.sha256(b"out").hexdigest())
        self.assertEqual(open(dest, "rb").read(), b"out"); self.assertEqual(len(sha), 64)
        self.cl.delete("j1"); self.assertFalse(os.path.exists(os.path.join(self.root, "j1")))
    def test_cannot_read_inputs_or_other_paths(self):
        self.cl.put("j1", self.f("a.mp4", b"x"))
        with self.assertRaises(x.XferError): self.cl.get("j1", "missing.mp4", os.path.join(self.tmp, "o"))
        for p in ("/v1/jobs/j1/in/a.mp4", "/v1/jobs/../etc/out/passwd", "/v1/jobs/j1/out/..%2Fin%2Fa.mp4",
                  "/v1/jobs/..%2F..%2Fx/out/a", "/etc/passwd", "/v1/jobs/.hidden/out/a"):
            self.assertIn(self.raw("GET", p), (404, 405, 400), p)
    def test_bad_signature_wrong_key(self):
        self.assertEqual(self.raw("GET", "/v1/ping", key=b"wrong" * 8), 401)
        self.assertFalse(x.XferClient(b"wrong", "127.0.0.1", self.srv.server_address[1], mode="direct").ping())
    def test_replay_and_stale(self):
        n = uuid.uuid4().hex; ts = int(time.time())
        self.assertEqual(self.raw("GET", "/v1/ping", ts=ts, nonce=n), 200)
        self.assertEqual(self.raw("GET", "/v1/ping", ts=ts, nonce=n), 401)
        self.assertEqual(self.raw("GET", "/v1/ping", ts=ts - 301), 401)
        self.assertEqual(self.raw("GET", "/v1/ping", ts=ts + 301), 401)
    def test_sha_mismatch_rejected(self):
        self.assertEqual(self.raw("PUT", "/v1/jobs/j2/in/a.mp4", body=b"data", sha="0" * 64), 422)
        self.assertEqual(os.listdir(os.path.join(self.root, "j2", "in")), [])
    def test_size_limits(self):
        self.srv.max_file = 10
        self.assertEqual(self.raw("PUT", "/v1/jobs/j3/in/a.mp4", body=b"x" * 11), 413)
        self.srv.max_file, self.srv.max_job = 100, 15
        self.assertEqual(self.raw("PUT", "/v1/jobs/j3/in/a.mp4", body=b"x" * 10), 200)
        self.assertEqual(self.raw("PUT", "/v1/jobs/j3/in/b.mp4", body=b"x" * 10), 413)
    def test_bad_names(self):
        with self.assertRaises(c.ValidationError): self.cl.put("../j", self.f("a.mp4", b"x"))
        with self.assertRaises(c.ValidationError): self.cl.get("j", "../a", "/tmp/x")
    def test_purge_old(self):
        self.cl.put("old", self.f("a.mp4", b"x"))
        x.purge_old(self.root, now=time.time() + 25 * 3600); self.assertEqual(os.listdir(self.root), [])

class JobFlow(Base):
    def test_validate_transport(self):
        j = job(transport="tailscale", input_sha256={"a.mp4": "a" * 64}); self.assertTrue(c.validate_job(j))
        for bad in (job(transport="tailscale"), job(transport="ftp"), job(input_sha256={"a.mp4": "a" * 64}),
                    job(transport="tailscale", input_sha256={"a.mp4": "zz"})):
            with self.assertRaises(c.ValidationError): c.validate_job(bad)
    def run_worker(self, gh, outbytes=b"rendered"):
        def fake(args, **kw):
            open(args[-1], "wb").write(outbytes); return mock.Mock(returncode=0, stderr="", stdout="")
        def wait(gh_, j, poll, timeout):
            with mock.patch("subprocess.run", side_effect=fake):
                w.run_job(gh_, j["id"], KEY, "/ff", worker="m", log=lambda m: None, xfer_dir=self.root)
            d, _ = gh_.get_file(f"jobs/{j['id']}/done.json"); f, _ = gh_.get_file(f"jobs/{j['id']}/failed.json")
            return d, f
        return mock.patch.object(s, "_wait", side_effect=wait)
    def test_submit_over_tailscale_end_to_end(self):
        gh = FakeGH(); inp = self.f("a.mp4", b"input!"); out = os.path.join(self.tmp, "outdir")
        with self.run_worker(gh):
            rc = s.submit(gh, job(), KEY, [inp], out, xfer=self.cl)
        self.assertEqual(rc, 0)
        self.assertEqual(open(os.path.join(out, "b.mp4"), "rb").read(), b"rendered")
        self.assertEqual(gh.releases, {})  # no release made
        done = json.loads(gh.files["jobs/t1/done.json"][0]); self.assertTrue(c.verify_obj(done, KEY))
        self.assertFalse(os.path.exists(os.path.join(self.root, "t1")))  # cleaned up on the Mac
    def test_worker_rejects_tampered_input(self):
        gh = FakeGH(); inp = self.f("a.mp4", b"input!")
        j = c.sign_job(job(transport="tailscale", input_sha256={"a.mp4": x.file_sha256(inp)}), KEY)
        gh.files["jobs/t1/job.json"] = (json.dumps(j).encode(), 1)
        self.cl.put("t1", self.f("a.mp4", b"swapped"))
        with mock.patch("subprocess.run") as run:
            w.run_job(gh, "t1", KEY, "/ff", log=lambda m: None, xfer_dir=self.root); run.assert_not_called()
        self.assertIn("sha256 mismatch", json.loads(gh.files["jobs/t1/failed.json"][0])["log_tail"])
    def test_fallback_to_github_when_unreachable(self):
        gh = FakeGH(); inp = self.f("a.mp4", b"in")
        dead = x.XferClient(KEY, "127.0.0.1", 1, mode="direct", timeout=1)
        with mock.patch.object(s, "_wait", return_value=({"exit_code": 0}, None)), \
             mock.patch.object(gh, "delete_release"):
            self.assertEqual(s.submit(gh, job(), KEY, [inp], self.tmp, xfer=dead), 0)
        self.assertNotIn("transport", json.loads(gh.files["jobs/t1/job.json"][0]))
    def test_cli_default_is_github(self):
        self.assertIn('default="github"', open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rh_submit.py")).read())

if __name__ == "__main__":
    unittest.main()
