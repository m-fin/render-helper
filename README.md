# Mac Render Helper (private)

Mac-only. A private, self-hosted worker you install on your Mac with one click, that lets your Grok Bot render files locally, saving valuable cloud compute.

Grok Bot's bots queue ffmpeg jobs; Frog's Mac pulls them, renders
locally (VideoToolbox), and sends results back. Both sides only connect **out** to GitHub.
Python 3 stdlib only. Free.

## How it works
1. `rh_submit.py` (bot side) builds `job.json` (`id, bot, created, cmd, inputs, outputs`),
   signs it with HMAC-SHA256, creates a prerelease `job-<id>`, uploads inputs as release
   assets, then commits `jobs/<id>/job.json`.
2. `rh_worker.py` (Mac) polls every 15 s, claims a job by creating `jobs/<id>/claimed.json`
   (create-only write; a race loses with 409/422 and is skipped), verifies the signature,
   validates args, runs ffmpeg in a temp dir, uploads outputs as assets, deletes input assets,
   writes `done.json` or `failed.json` (exit code + log tail).
3. The submitter downloads outputs and deletes the release + tag. Only tiny JSON is ever committed.

Safety: only `ffmpeg`/`ffprobe`; no shell; args with absolute paths, `..`, `~`, or any protocol
other than `file:` (http, tcp, pipe, concat, …) and `movie=`/`sendcmd` filters are rejected;
files only via `{in:NAME}` / `{out:NAME}` placeholders inside the job dir; size caps
(4 GB inputs, 1.9 GB per output); 2 h timeout; unsigned jobs refused. `libx264`/`libx265` are
swapped for `h264_videotoolbox`/`hevc_videotoolbox` (disable with `--no-videotoolbox`).

## Install on the Mac (one step)
1. **Token.** github.com → Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token. Resource owner `m-fin`; Repository access
   **Only select repositories → m-fin/render-helper**; Repository permissions
   **Contents: Read and write** only (covers releases, release assets and tags; Metadata
   read-only is added automatically). 90-day expiry is fine.
2. **Get the files:** repo page → Code → Download ZIP (or `git clone https://github.com/m-fin/render-helper.git`).
3. **Run the installer in Terminal** from that folder:
   ```
   bash install.command
   ```
   It copies the worker to `~/Library/Application Support/macrenderhelper`, checks for Homebrew
   ffmpeg, asks once for the token and the HMAC key (press Enter to generate a key; it is
   printed once, so give it to Grok Bot in your private 1:1 chat as `RH_HMAC_KEY`), saves both
   to the macOS Keychain, installs and loads the launchd agent, then runs a 1-second 64×64
   test render and prints **PASS** or **FAIL**. Safe to re-run (keeps existing Keychain items).
   If macOS asks to let python3 use the Keychain items, click **Always Allow**.
4. Uninstall: `bash install.command --uninstall`. Log: `~/Library/Logs/macrenderhelper.log`.

Running it with `bash` avoids Gatekeeper quarantine. On macOS 15+ a double-clicked unsigned
`install.command` is blocked: allow it in System Settings → Privacy & Security → Open Anyway,
or run `xattr -d com.apple.quarantine install.command`. No `curl | bash` install while the
repo is private. (`com.mfin.macrenderhelper.plist` is a reference template; the installer writes
its own.)

## Bot side (Grok Bot's computer)
The bots need their **own** fine-grained token (same settings as step 1; a second token so
either can be revoked alone) because the GitHub connector cannot upload release assets.
Frog provides it through a secret request in his 1:1 chat with Grok Bot, exposed as env var
`RH_GITHUB_TOKEN`, plus `RH_HMAC_KEY` from step 4. Nothing is stored in this repo.

End-to-end test (tiny, ~1 s, 64×64, rendered on the Mac):
```
python3 rh_submit.py --bot test -o t.mp4 -- ffmpeg -f lavfi -i testsrc=d=1:s=64x64 -c:v libx264 {out:t.mp4}
```
Real job: `python3 rh_submit.py --bot video-creator -i frames.zip -o out.mp4 -- ffmpeg -i {in:frames.zip} ... {out:out.mp4}`

## Handshake (pairing + heartbeat)
- **Pairing, once:** on first run the worker makes a stable id (`mac-xxxxxxxxxxxx`, saved as
  `worker_id` in the install dir) and commits one HMAC-signed `workers/<id>/hello.json`:
  `worker_id, model` (`sysctl hw.model`), `macos` (`sw_vers`), `chip`
  (`machdep.cpu.brand_string`), `arch`, `ffmpeg` (first line of `ffmpeg -version`), `at`.
  Create-only, so re-runs never add commits.
- **Heartbeat, every 60 s, no commits:** a signed `heartbeat-<id>.json` (`at, state idle|busy,
  job, model, ffmpeg`) is uploaded as an asset on the prerelease tag `worker-status`; the old
  copy is deleted. Runs in its own thread, so a long render still reports `busy`.
- **Bot side:** `python3 rh_status.py` verifies both signatures and prints ONLINE/OFFLINE
  (offline after 10 min without a valid heartbeat) with model, macOS, chip, ffmpeg.
  `python3 rh_status.py --test-job` sends the tiny 1 s 64×64 test job if a worker is online.

## Stats and dashboard
After each job the worker appends one line to `stats/jobs.jsonl` (also copied into
`done.json`/`failed.json`): `wall_s` (measured), `cpu_s` (ffmpeg user+sys from getrusage, or
null), `input_bytes`, `output_bytes`, and `cloud_cpu_minutes_saved_estimate`
`{value, is_estimate: true, formula}`. **That figure is an estimate:** `cpu_s / 60 × 1.0`
(`wall_s` if CPU time is missing); the 1.0 factor is an assumption, not a measurement.
`python3 rh_dashboard.py -o status.html` (or `--jsonl FILE` offline) writes a static page and
prints a one-line summary.

## Tests
`python3 -m unittest -v test_rh` (ffmpeg and HTTP are mocked; nothing renders).
