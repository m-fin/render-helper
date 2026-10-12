// Fixed browser-capture script for Mac Render Helper (committed in the repo; jobs cannot change it).
// node rh_capture.mjs <gameDir> <seed> <W> <H> <out.mp4> <limitFrames> <ffmpegPath>
// Serves <gameDir> on 127.0.0.1 only, drives Frog's installed Chrome via Playwright (channel 'chrome',
// no browser download), blocks every non-local request, and pipes frame-locked JPEGs to ffmpeg.
// Game contract: <gameDir>/capture.html exposes window.cap.init(seed,W,H,events) / cap.frame(2),
// and <gameDir>/runs/events_seed<seed>.json holds the event log with a run_end tick.
import fs from 'fs'; import http from 'http'; import path from 'path'; import { spawn } from 'child_process';
import { createRequire } from 'module'; import { fileURLToPath } from 'url';
const here = path.dirname(fileURLToPath(import.meta.url));
const { chromium } = createRequire(path.join(here, 'package.json'))('playwright-core');
const [gameDir, seedS, WS, HS, out, limS, ffmpeg] = process.argv.slice(2);
const seed = +seedS, W = +WS, H = +HS, LIMIT = +limS || 0;
const root = fs.realpathSync(gameDir);
const run = JSON.parse(fs.readFileSync(path.join(root, 'runs', `events_seed${seed}.json`)));
const endTick = run.events.find(e => e.type === 'run_end').tick;
const N = LIMIT || Math.round(endTick / 2);
const types = { '.html': 'text/html', '.mjs': 'text/javascript', '.js': 'text/javascript', '.json': 'application/json' };
const srv = http.createServer((q, s) => {
  const f = path.resolve(root, '.' + decodeURI(q.url.split('?')[0]));
  if (!f.startsWith(root + path.sep)) { s.writeHead(403); return s.end(); }
  fs.readFile(f, (e, d) => { if (e) { s.writeHead(404); return s.end(); } s.writeHead(200, { 'content-type': types[path.extname(f)] || 'application/octet-stream' }); s.end(d); });
});
await new Promise(r => srv.listen(0, '127.0.0.1', r));
const origin = `http://127.0.0.1:${srv.address().port}`;
const browser = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-angle=metal', '--ignore-gpu-blocklist'] });
const page = await browser.newPage({ viewport: { width: W, height: H } });
await page.route('**/*', r => r.request().url().startsWith(origin + '/') ? r.continue() : r.abort());
page.on('pageerror', e => console.error('[pageerr]', e.message));
await page.goto(`${origin}/capture.html`); await page.waitForFunction(() => window.cap);
const first = await page.evaluate(([s, w, h, e]) => window.cap.init(s, w, h, e), [seed, W, H, run.events]);
const enc = (vt) => ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', '30', '-c:v', 'mjpeg', '-i', '-',
  ...(vt ? ['-c:v', 'h264_videotoolbox', '-b:v', W * H > 1e6 ? '12M' : '6M'] : ['-c:v', 'libx264', '-crf', '20']),
  '-pix_fmt', 'yuv420p', '-r', '30', '-movflags', '+faststart', out];
const lossless = ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', '30', '-c:v', 'mjpeg', '-i', '-', '-c:v', 'ffv1', '-level', '3', '-pix_fmt', 'yuv420p', '-r', '30', out];
const ff = spawn(ffmpeg, out.endsWith('.mkv') ? lossless : enc(process.platform === 'darwin'), { stdio: ['pipe', 'inherit', 'inherit'] });
const write = (b) => new Promise(r => ff.stdin.write(b) ? r() : ff.stdin.once('drain', r));
const t0 = Date.now();
for (let k = 0; k < N; k++) {
  const info = k === 0 ? first : await page.evaluate(() => window.cap.frame(2));
  if (info.tick !== 2 * k) throw new Error(`tick drift at frame ${k}: ${info.tick}`);
  await write(Buffer.from(info.jpg, 'base64'));
  if (k % 150 === 0) console.error(`frame ${k}/${N} ${((Date.now() - t0) / 1000).toFixed(0)}s`);
}
ff.stdin.end(); const code = await new Promise(r => ff.on('close', r)); await browser.close(); srv.close();
console.log(JSON.stringify({ seed, W, H, frames: N, video_s: N / 30, run_end_s: run.duration_s, capture_s: (Date.now() - t0) / 1000 }));
process.exit(code);
