# Net of Fees — automated video pipeline

Turns research into published video. Charts are rendered from data, the
narration is generated in your own cloned voice, and two human gates sit
between the script and the world.

Runs on **Windows and macOS**. Nothing in the render path touches the
network: once a snapshot and script exist, you can render with the wifi off.

Full design: the architecture doc, 28 sections. This file is setup only.

---

## What you need to do

Four things. Only the first two block you.

| # | Task | Why | Time |
| - | ---- | --- | ---- |
| 1 | Install Python 3.11 and FFmpeg | Nothing runs without them | 10 min |
| 2 | Run `setup.sh` / `setup.ps1` | Creates the venv, installs deps | 2 min |
| 3 | **Apply for YouTube API audit** | Longest lead time. Until it passes, every upload is force-locked to private | 20 min, then wait |
| 4 | Record your voice reference | 30–60 min, one session, one room, one mic position | 1 hour |

You can render complete videos after step 2. Steps 3 and 4 are only needed
to publish and to replace the draft voice.

---

## Setup — Windows

```powershell
winget install Python.Python.3.11
winget install Gyan.FFmpeg
# close and reopen the terminal so PATH refreshes
cd net-of-fees
.\setup.ps1
```

If PowerShell blocks the script:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

**Use Python 3.11 or 3.12, not 3.13+.** The audio stack has no wheels for
3.13 or 3.14 yet, and pip will try to build from source and fail.

## Setup — macOS

```bash
brew install python@3.11 ffmpeg
cd net-of-fees
bash setup.sh
```

## Check it

```bash
python run.py doctor
```

Reports your OS, Python, FFmpeg, compute device and font. Anything
missing is named with the command that fixes it.

---

## First video

```bash
python run.py new --recipe fee_drag    # creates episode 001 + data snapshot
python run.py 001                      # script → scrub → STOPS at gate 1
```

Read `episodes/001/script.md` against `episodes/001/input/data.json`.
Every number in the script must appear in the data. This is where a wrong
figure gets caught.

```bash
python run.py 001 --approve-script     # voice → charts → captions → assemble
```

Watch `episodes/001/out/video.mp4`, pick a thumbnail from `out/thumb_*.png`.

```bash
python run.py 001 --approve            # cuts the Shorts
python run.py 001 --publish --dry-run  # shows exactly what would upload
python run.py 001 --publish            # uploads for real
```

On a laptop an 8-minute video takes roughly 10–20 minutes to render.

## Other commands

```bash
python run.py list                       # every episode and its progress
python run.py 001 --status               # one episode in detail
python run.py 001 --from charts          # resume from a stage
python run.py 001 --stage assemble --force   # re-run one stage
python run.py 001 --voice-engine cloned  # use a real voice
python run.py 001 --captions whisper     # true word-level timing
pytest                                   # 57 tests, no network needed
```

---

## Voice

The pipeline ships with a **draft engine** that generates silence of the
right length. It exists so you can verify assembly, timing and captions
before downloading any model. **Draft audio can never be published** —
gate 2 and the upload stage both refuse it.

### An interim voice, before you record

Kokoro ships named stock voices under Apache 2.0 - commercial use is
permitted, and they are model voices rather than clones of anyone.

```bash
pip install -r requirements-voice.txt
python run.py voices                          # see them all
python tools/setup_stock_voice.py af_heart    # pick one
python run.py 001 --voice-engine kokoro --voice stock_af_heart \
               --stage voice --force
```

Output from a stock voice **can** be published. Only the `silent` draft
engine cannot.

**Never clone a voice found online.** A creator's voice, a public
figure's, anyone's - that is impersonation under YouTube policy and
carries personality-rights exposure. Section 14 of the architecture doc
makes it a hard rule, and `consent.md` enforces it per voice.

### To use your real voice

1. Generate your recording script and follow it:
   ```bash
   python tools/make_voice_script.py > voices/chandan_en/RECORDING.md
   ```
   45-60 minutes, one session, one room, one mic position. Five blocks:
   warm-up, your real scripts, vocabulary and numbers, delivery range,
   and unscripted speech. **Leave your breaths in.**
2. **Confirm your cloning model's checkpoint licence permits commercial
   use.** Several good open models (XTTS-v2, some F5-TTS and Fish Speech
   checkpoints) are non-commercial and would be a licence violation on a
   monetised channel. Chatterbox (MIT) is the starting candidate.
3. `pip install -r requirements-voice.txt`
4. `python run.py 001 --voice-engine cloned --stage voice --force`

Every voice directory needs a `consent.md`. Cloning your own voice is
yours to do; any other voice needs the speaker's documented permission.
A voice without one fails validation and the episode will not render.

---

## Publishing

```bash
pip install -r requirements-publish.txt
```

1. Google Cloud console → new project
2. Enable **YouTube Data API v3**
3. Create an OAuth client ID, type **Desktop app**
4. Download it to `secrets/client_secret.json`
5. `python tools/youtube_auth.py`

**Apply for the API audit at step 2.** Until it passes, uploads are
force-locked to private no matter what you set. It has the longest lead
time in the whole project, so start the clock while you build.

Quota: one upload costs ~1,600 of a 10,000 daily allowance — about six
uploads a day.

---

## Layout

```
pipeline/          stages, manifest, scrub, platform support
  stages/          export → script → scrub → voice → charts →
                   captions → assemble → thumbnail → shorts → upload
  voices/          engine interface: silent (draft), kokoro, cloned
  recipes/         one query + one chart set + one script template
profiles/
  trading/         charts, brand, script template, 16:9
  trading_shorts/  same data redrawn natively vertical, 9:16
voices/            one directory per voice, each with consent.md
episodes/          one directory per episode, all its files
tests/             57 tests, none of which touch the network
```

### Where to change things

| Want to change | Edit |
| --- | --- |
| Colours, fonts, type scale | `profiles/trading/style.py` |
| Chart types | `profiles/trading/visuals.py` |
| Script tone and structure | `profiles/trading/script.md` |
| Pronunciation | `voices/<name>/lexicon.json` |
| Resolution, loudness, tolerances | `pipeline/config.py` |
| A new topic entirely | A new `profiles/<name>/` — the core is topic-agnostic |

---

## Two things that will bite

**Rates go stale.** `pipeline/recipes/fee_drag.py` carries a rate card with
a `verify_before_publish` list. Charges change with budgets and circulars.
Confirm them before publishing or you publish a wrong number.

**There is no flag that skips a gate.** Both gates are enforced in code,
not in a UI. If one is ever added, the architecture doc is being violated.
