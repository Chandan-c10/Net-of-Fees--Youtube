"""A local control panel for the whole pipeline.

Why this exists: the gates are the one part that cannot be automated, and
clearing them from a terminal means reading a script in a log file and
trusting that the thumbnail is fine. This serves the same decisions as
pages -- the script beside its charts, the video beside its thumbnails,
the reels beside their captions -- and then calls the identical code the
CLI calls.

It does not re-implement the gate conditions. `pipeline.gates` owns them
and both callers share it, because a gate implemented twice is a gate
that will eventually disagree with itself. Stages run by spawning
`run.py`, so the panel exercises the same path a terminal would and a
crashed render cannot take the panel with it.

Security, stated plainly. This panel can publish to your channel. So:

  * It binds to 127.0.0.1 by default. Nothing outside the machine can
    reach it, and no password is needed because there is no exposure to
    protect against.
  * Binding anywhere else REQUIRES a token and the server refuses to
    start without one. You get an unguessable link, which is the
    "no sign-in" experience asked for without the panel being
    world-writable.
  * Mutations are POST only, carry the token in the form body, and are
    refused when the Origin header belongs to another site. A link
    pasted into a chat app cannot approve an episode.
  * Publishing for real needs a typed confirmation, because it is the
    one action that cannot be undone.

Stdlib only, deliberately: this has to start on a Windows laptop with no
extra install.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import archive, config, gates, panel_auth, platform_support, prune, settings_store
from .errors import GateError, PipelineError
from .logging_setup import get
from .manifest import STAGES, Episode, Manifest

EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

# Assets the panel will serve out of an episode directory. Anything else
# is refused, so a traversal cannot reach a manifest or a token file.
SERVABLE = {".png", ".jpg", ".jpeg", ".mp4", ".webm", ".wav", ".srt"}

LOG_LINES = 400
JOB_TIMEOUT_SEC = 3600


# ---- running stages ---------------------------------------------------


class Job:
    """One `run.py` invocation, with its output kept for the page.

    A render takes a minute or more, which no HTTP request should wait
    for, so the work happens on a thread and the page polls for the log.
    """

    def __init__(self, episode_id: str, argv: list[str], label: str) -> None:
        self.episode_id = episode_id
        self.argv = argv
        self.label = label
        self.lines: deque[str] = deque(maxlen=LOG_LINES)
        self.started = time.time()
        self.finished: float | None = None
        self.returncode: int | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.finished is None

    def run(self) -> None:
        root = Path(__file__).resolve().parent.parent
        cmd = [sys.executable, str(root / "run.py"), *self.argv]
        self._append(f"$ run.py {' '.join(self.argv)}")
        try:
            self._proc = subprocess.Popen(
                cmd, cwd=str(root), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace",
            )
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                self._append(line.rstrip())
            self.returncode = self._proc.wait(timeout=JOB_TIMEOUT_SEC)
        except Exception as exc:  # surfaced on the page, not raised
            self._append(f"job failed to run: {type(exc).__name__}: {exc}")
            self.returncode = -1
        finally:
            self.finished = time.time()
            self._append(
                f"-- finished with exit {self.returncode} in "
                f"{self.finished - self.started:.1f}s"
            )

    def cancel(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            self._append("-- cancelled")

    def _append(self, line: str) -> None:
        with self._lock:
            self.lines.append(line)

    def snapshot(self) -> dict:
        with self._lock:
            lines = list(self.lines)
        return {
            "episode": self.episode_id,
            "label": self.label,
            "running": self.running,
            "returncode": self.returncode,
            "elapsed": round((self.finished or time.time()) - self.started, 1),
            "lines": lines,
        }


# ---- state ------------------------------------------------------------


class Panel:
    """Shared state for the handler, which http.server instantiates per
    request and so cannot hold any itself."""

    def __init__(self, episodes_dir: Path, token: str | None) -> None:
        self.episodes_dir = episodes_dir
        self.token = token
        self.log = get(None, "dashboard")
        self.lock = threading.Lock()
        self.job: Job | None = None
        # Episodes approved with publish_mode == "on_approval". The
        # publish is started after the lock is released, because
        # start_job takes the same lock.
        self._auto_publish: list[str] = []
        # Optional. With no password set, a localhost panel behaves as it
        # always has; setting one makes it required everywhere.
        self.credentials = panel_auth.Credentials()
        self.sessions = panel_auth.Sessions()

    # ---- episodes -----------------------------------------------------

    def episode_ids(self) -> list[str]:
        if not self.episodes_dir.exists():
            return []
        return sorted(
            (d.name for d in self.episodes_dir.iterdir()
             if d.is_dir() and EPISODE_ID.match(d.name)
             and (d / "manifest.json").exists()),
            reverse=True,
        )

    def load(self, episode_id: str) -> tuple[Episode, Manifest]:
        if not EPISODE_ID.match(episode_id):
            raise PipelineError(f"bad episode id {episode_id!r}")
        ep = Episode(episode_id, self.episodes_dir)
        if not ep.manifest_path.exists():
            raise PipelineError(f"no episode {episode_id}")
        return ep, ep.load_manifest()

    def summary(self, episode_id: str) -> dict:
        ep, m = self.load(episode_id)
        return {
            "id": m.id,
            "title": m.title or "(untitled)",
            "profile": m.profile,
            "tier": m.tier,
            "recipe": m.recipe,
            "voice": m.voice,
            "stages": {s: _stage_state(m, s) for s in STAGES},
            "gate_1": bool(m.gate_1_passed_at),
            "gate_2": bool(m.gate_2_passed_at),
            "published": bool(m.published_at),
            "gate_1_blockers": gates.gate_1_blockers(ep, m),
            "gate_2_blockers": gates.gate_2_blockers(m),
            "chosen_thumbnail": m.chosen_thumbnail,
            "publish_mode": m.publish_mode,
            "keep_files": m.keep_files,
            "publish_after": m.publish_after,
            "draft_voice": bool(m.stage("voice").data.get("draft")),
            "scrub_findings": m.stage("scrub").data.get("findings", 0),
            "duration_sec": m.stage("assemble").data.get("duration_sec"),
            "drift_sec": m.stage("assemble").data.get("drift_sec"),
            "platform_posts": m.platform_posts,
        }

    # ---- gate actions -------------------------------------------------

    def approve(self, episode_id: str, which: int) -> str:
        """Clear a gate. The lock keeps two browser tabs from racing on
        the same manifest, which would lose one of the writes."""
        with self.lock:
            ep, m = self.load(episode_id)
            if which == 1:
                gates.approve_gate_1(ep, m)
                return f"Gate 1 cleared for {episode_id}."
            gates.approve_gate_2(ep, m)
            message = f"Gate 2 cleared for {episode_id}."
            if m.publish_mode == "on_approval":
                self._auto_publish.append(episode_id)
                message += " Publishing now."
            elif m.publish_mode == "at_slot":
                when = (m.publish_after or "")[:16].replace("T", " ")
                message += f" Will publish at {when}."
            return message

    def set_keep(self, episode_id: str, keep: bool) -> str:
        """Exempt this episode from retention, or stop exempting it."""
        with self.lock:
            ep, m = self.load(episode_id)
            m.keep_files = keep
            ep.save_manifest(m)
            return ("This episode is kept whole." if keep
                    else "This episode follows the retention settings.")

    def set_publish_mode(self, episode_id: str, mode: str) -> str:
        """How this episode leaves once approved.

        Never touches the gate. A person still approves every episode;
        this only decides whether a second manual action is needed after
        that.
        """
        from .manifest import PUBLISH_MODES
        if mode not in PUBLISH_MODES:
            raise PipelineError(
                f"unknown publish mode {mode!r}; expected "
                f"{', '.join(PUBLISH_MODES)}")
        with self.lock:
            ep, m = self.load(episode_id)
            m.publish_mode = mode
            if mode == "at_slot" and m.approved_at and not m.publish_after:
                from .schedule import next_slot
                m.publish_after = next_slot().isoformat()
            ep.save_manifest(m)
            return {
                "manual": "Will wait for you to press publish.",
                "on_approval": "Will publish as soon as it is approved.",
                "at_slot": "Will publish at the next slot after approval.",
            }[mode]

    def choose_thumbnail(self, episode_id: str, n: int) -> str:
        with self.lock:
            ep, m = self.load(episode_id)
            if not ep.thumbnail(n).exists():
                raise PipelineError(f"no thumbnail {n} for {episode_id}")
            m.chosen_thumbnail = n
            ep.save_manifest(m)
            return f"Thumbnail {n} chosen."

    def choose_title(self, episode_id: str, text: str) -> str:
        """Pick among the candidates metadata produced.

        Only a candidate may be chosen: free text here would let a title
        past the advisory-language check that metadata applies.
        """
        with self.lock:
            ep, m = self.load(episode_id)
            meta_path = ep.dir / "metadata.json"
            if not meta_path.exists():
                raise PipelineError("run the metadata stage first")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            allowed = {c["text"] for c in meta.get("title_candidates", [])}
            if text not in allowed:
                raise PipelineError(
                    "only a generated candidate can be chosen; they are the "
                    "ones checked for advisory language"
                )
            meta["chosen_title"] = text
            meta.setdefault("youtube", {})["title"] = text[:100]
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            m.title = text
            ep.save_manifest(m)
            return f"Title set to {text!r}."

    def set_flag(self, episode_id: str, index: int, flagged: bool) -> str:
        """Flag a scene. Gate 1 refuses while any scene is flagged, which
        is the designed way to park an episode you are unhappy with."""
        with self.lock:
            ep, m = self.load(episode_id)
            scene = m.scene(index)
            scene.flagged = flagged
            ep.save_manifest(m)
            return (f"Scene {index} flagged." if flagged
                    else f"Scene {index} cleared.")

    # ---- jobs ---------------------------------------------------------

    def drain_auto_publish(self) -> str | None:
        """Start a publish for anything approved with on_approval set.

        Called after `approve` returns, outside its lock. Returns a note
        to append to the message, or None.
        """
        if not self._auto_publish:
            return None
        episode_id = self._auto_publish.pop(0)
        try:
            return self.start_job(episode_id, [episode_id, "--publish"],
                                  "publish on approval")
        except PipelineError as exc:
            return f"Could not start publishing: {exc}"

    def start_job(self, episode_id: str, argv: list[str], label: str) -> str:
        """One job at a time: renders are CPU-bound, and two of them on
        one episode would interleave writes to the same manifest."""
        with self.lock:
            if self.job and self.job.running:
                raise PipelineError(
                    f"already running {self.job.label!r} on "
                    f"{self.job.episode_id}. Wait for it, or cancel it."
                )
            job = Job(episode_id, argv, label)
            self.job = job
        threading.Thread(target=job.run, daemon=True).start()
        return f"Started: {label}."

    def cancel_job(self) -> str:
        with self.lock:
            if not (self.job and self.job.running):
                raise PipelineError("nothing is running")
            self.job.cancel()
        return "Cancelling."

    def job_snapshot(self) -> dict | None:
        job = self.job
        return job.snapshot() if job else None

    # ---- creation -----------------------------------------------------

    def create(self, episode_id: str, profile: str, recipe: str) -> str:
        import profiles

        from .recipes import available as available_recipes
        if episode_id and not EPISODE_ID.match(episode_id):
            raise PipelineError(
                "an episode id may use letters, digits, dash and underscore, "
                "up to 32 characters"
            )
        if profile not in profiles.available():
            raise PipelineError(f"no profile {profile!r}")
        if recipe not in available_recipes():
            raise PipelineError(f"no recipe {recipe!r}")
        argv = ["new", "--profile", profile, "--recipe", recipe]
        if episode_id:
            argv += ["--id", episode_id]
        return self.start_job(episode_id or "new", argv, f"new {profile}")

    # ---- assets -------------------------------------------------------

    def asset(self, episode_id: str, rel: str) -> Path:
        """Resolve an asset path, refusing anything outside the episode."""
        ep, _ = self.load(episode_id)
        root = ep.dir.resolve()
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise PipelineError("path outside the episode directory")
        if target.suffix.lower() not in SERVABLE:
            raise PipelineError(f"{target.suffix} is not servable")
        if not target.is_file():
            raise PipelineError("no such asset")
        return target


@dataclass(frozen=True)
class Standing:
    """Where an episode sits, from the operator's point of view.

    The sidebar in most tools lists features. This lists states, because
    the only question someone opens this panel to answer is "what needs
    me now" -- and an episode either needs a decision, is working, or is
    finished.
    """

    group: str        # "needs_you" | "running" | "done"
    label: str        # what is true
    detail: str       # why, in one line
    order: int        # within the group; lower is more urgent


# Ordered by how much they cost to ignore. A failure that has stopped the
# line outranks a script nobody has read yet.
FAILED, GATE_2, GATE_1, BLOCKED, PUBLISHABLE = 0, 1, 2, 3, 4


def standing_of(summary: dict, running: bool) -> Standing:
    """Classify one episode. Pure, so it is testable without a server."""
    if running:
        return Standing("running", "working", "a job is running", 0)

    failed = [name for name, state in summary["stages"].items()
              if state == "failed"]
    if failed:
        return Standing("needs_you", "failed",
                        f"{failed[0]} stopped; it needs a person", FAILED)

    if summary["published"]:
        return Standing("done", "published", "live", 0)

    if summary["gate_2"]:
        return Standing("needs_you", "ready to publish",
                        "approved and waiting to go out", PUBLISHABLE)

    # A gate with no blockers is a decision waiting. A gate with blockers
    # is work waiting, and saying which is the whole point.
    if summary["gate_1"]:
        blockers = summary["gate_2_blockers"]
        if not blockers:
            return Standing("needs_you", "video ready",
                            "watch it and approve", GATE_2)
        return Standing("running" if _is_automatic(blockers) else "needs_you",
                        "rendering" if _is_automatic(blockers) else "blocked",
                        blockers[0], BLOCKED)

    blockers = summary["gate_1_blockers"]
    if not blockers:
        return Standing("needs_you", "script ready",
                        "read it and approve", GATE_1)
    return Standing("running" if _is_automatic(blockers) else "needs_you",
                    "drafting" if _is_automatic(blockers) else "blocked",
                    blockers[0], BLOCKED)


# Blockers that clear themselves by running the next stage, rather than
# by someone doing something. These are progress, not problems.
_AUTOMATIC = ("is not complete", "has not passed", "no scenes",
              "data snapshot is missing")


def _is_automatic(blockers: list[str]) -> bool:
    return all(any(hint in b for hint in _AUTOMATIC) for b in blockers)


def _stage_state(m: Manifest, name: str) -> str:
    st = m.stage(name)
    if st.done:
        return "done"
    if st.error:
        return "failed"
    if m.gate_blocking(name):
        return "gated"
    return "todo"


def health() -> dict:
    """What `doctor` reports, for the page."""
    from . import notify
    info = dict(platform_support.describe_environment())
    ch = notify.Channel()
    info["alerts"] = (f"to {ch.to}" if ch.configured
                      else "not configured: " + ", ".join(ch.missing()))
    info["youtube"] = (
        "authorised" if Path(os.getenv(
            "YOUTUBE_TOKEN_FILE", "secrets/youtube_token.json")).exists()
        else "not authorised (run tools/youtube_auth.py)")
    info["instagram"] = ("configured" if os.getenv("IG_USER_ID")
                         and os.getenv("IG_ACCESS_TOKEN")
                         else "not configured")
    return info


# ---- rendering --------------------------------------------------------

STYLE = """
:root {
  --bg:#f5f5f2; --card:#fff; --ink:#191918; --muted:#6a6a64; --line:#e3e3dd;
  --ok:#1d7a4c; --warn:#8f5600; --bad:#a32a2a; --accent:#1c3f9e;
  --okbg:#eaf5ee; --badbg:#fbeeee; --r:10px;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#14151a; --card:#1c1e23; --ink:#e9e9e5; --muted:#9b9b94;
    --line:#2c2f36; --ok:#4cba7e; --warn:#d6943f; --bad:#e26c6c;
    --accent:#86a4ff; --okbg:#16251d; --badbg:#2a1a1a;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:28px 16px 90px}
h1{font-size:1.65rem;margin:0 0 4px;letter-spacing:-.01em}
h2{font-size:1.2rem;margin:38px 0 12px}
h3{font-size:1rem;margin:0 0 10px}
a{color:var(--accent)}
.sub{color:var(--muted);margin:0 0 26px;font-size:.95rem}
.card{background:var(--card);border:1px solid var(--line);
  border-radius:var(--r);padding:20px;margin-bottom:16px}
.row{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap}
.grow{flex:1 1 240px;min-width:0}
.pills{display:flex;flex-wrap:wrap;gap:5px;margin-top:10px}
.pill{font-size:12px;line-height:1.5;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pill.done{color:var(--ok);border-color:var(--ok)}
.pill.failed{color:var(--bad);border-color:var(--bad)}
.pill.gated{color:var(--warn);border-color:var(--warn)}
.tag{font-size:13px;padding:2px 10px;border-radius:999px;
  border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag.ok{color:var(--ok);border-color:var(--ok)}
.tag.open{color:var(--warn);border-color:var(--warn)}
.tag.bad{color:var(--bad);border-color:var(--bad)}
button,.btn{font:inherit;font-size:15px;font-weight:600;padding:10px 18px;
  border-radius:8px;border:1px solid transparent;background:var(--accent);
  color:#fff;cursor:pointer;text-decoration:none;display:inline-block;
  transition:opacity 120ms ease,transform 120ms ease}
button:hover:not(:disabled){opacity:.9}
button:active:not(:disabled){transform:translateY(1px)}
button:disabled{background:var(--line);color:var(--muted);cursor:not-allowed}
button.ghost{background:transparent;color:var(--accent);border-color:var(--line)}
button.danger{background:var(--bad)}
button.small{font-size:13px;padding:6px 12px}
.acts{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.acts form{margin:0}
.blockers{margin:12px 0 0;padding-left:20px;color:var(--bad);font-size:.95rem}
.blockers li{margin:4px 0}
.scene{display:flex;gap:18px;padding:18px 0;border-top:1px solid var(--line)}
.scene:first-of-type{border-top:0;padding-top:0}
.scene.flagged{background:var(--badbg);border-radius:8px;padding:14px;
  margin-top:10px;border-top:0}
.scene img{width:250px;max-width:40vw;border-radius:8px;border:1px solid var(--line)}
.scene p{margin:6px 0 10px}
.n{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
audio{width:100%;max-width:330px;height:34px;margin-top:4px}
.thumbs,.reels{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px}
.thumb{border:0;background:none;padding:0;cursor:pointer}
.thumb img{width:225px;max-width:42vw;display:block;border-radius:8px;
  border:3px solid transparent;transition:border-color 120ms ease}
.thumb.picked img{border-color:var(--ok)}
.thumb span{display:block;font-size:13px;color:var(--muted);margin-top:6px}
.reel{flex:0 0 190px}
.reel video{width:190px;border-radius:8px;background:#000;display:block}
video.main{width:100%;border-radius:8px;background:#000}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);
  border:1px solid var(--line);border-radius:8px;padding:13px;
  font-size:13.5px;line-height:1.6;margin:0;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre.log{max-height:330px;overflow:auto;background:#101216;color:#d6d6d0;
  border-color:#262a31}
.flash{padding:13px 16px;border-radius:8px;margin-bottom:20px;
  border:1px solid var(--ok);background:var(--okbg);color:var(--ok);font-size:.95rem}
.flash.bad{border-color:var(--bad);background:var(--badbg);color:var(--bad)}
.empty{color:var(--muted)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:8px 18px;font-size:14.5px}
.kv dt{color:var(--muted)}
.kv dd{margin:0;word-break:break-word}
label{display:block;font-size:13px;color:var(--muted);margin:0 0 4px}
select,input[type=text]{font:inherit;font-size:15px;padding:9px 11px;
  border-radius:8px;border:1px solid var(--line);background:var(--card);
  color:var(--ink);min-width:160px}
.fields{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-bottom:14px}
.spin{display:inline-block;width:11px;height:11px;border-radius:50%;
  border:2px solid var(--line);border-top-color:var(--accent);
  animation:spin .8s linear infinite;vertical-align:-1px;margin-right:6px}
@keyframes spin{to{transform:rotate(360deg)}}
@media (max-width:620px){
  .scene{flex-direction:column}
  .scene img{width:100%;max-width:none}
  .reel{flex:1 1 44%}.reel video{width:100%}
}
"""

POLL_JS = """
(function(){
  var box=document.getElementById('log'), meta=document.getElementById('jobmeta');
  if(!box) return;
  var at_bottom=true;
  box.addEventListener('scroll',function(){
    at_bottom = box.scrollTop+box.clientHeight >= box.scrollHeight-20;
  });
  function tick(){
    fetch(JOB_URL,{cache:'no-store'}).then(function(r){return r.json()})
      .then(function(j){
        if(!j){return}
        box.textContent=j.lines.join('\\n');
        if(at_bottom){box.scrollTop=box.scrollHeight}
        meta.textContent=j.running
          ? j.label+' - running '+j.elapsed+'s'
          : j.label+' - exit '+j.returncode+' after '+j.elapsed+'s';
        if(j.running){setTimeout(tick,1200)}
        else{setTimeout(function(){location.reload()},900)}
      }).catch(function(){setTimeout(tick,3000)});
  }
  tick();
})();
"""


def _page(title: str, body: str, poll_url: str | None = None) -> bytes:
    script = (f"<script>var JOB_URL={json.dumps(poll_url)};{POLL_JS}</script>"
              if poll_url else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{STYLE}</style></head>
<body><div class="wrap">{body}</div>{script}</body></html>""".encode()


def _tok(token: str | None) -> str:
    if not token:
        return ""
    return f'<input type="hidden" name="token" value="{html.escape(token)}">'


def _q(token: str | None, **extra: str) -> str:
    params = {}
    if token:
        params["token"] = token
    params.update(extra)
    return f"?{urllib.parse.urlencode(params)}" if params else ""


def _flash(flash: tuple[str, bool]) -> str:
    message, ok = flash
    return f'<div class="flash{"" if ok else " bad"}">{html.escape(message)}</div>'


def _esc(value: object) -> str:
    return html.escape(str(value))


def _post(action: str, token: str | None, label: str,
          fields: str = "", cls: str = "", confirm: str = "",
          disabled: bool = False) -> str:
    attrs = f' class="{cls}"' if cls else ""
    if disabled:
        attrs += ' disabled title="a job is already running"'
    guard = (f' onsubmit="return confirm({json.dumps(confirm)})"'
             if confirm else "")
    return (f'<form method="post" action="{action}"{guard}>{_tok(token)}'
            f'{fields}<button type="submit"{attrs}>{label}</button></form>')


def _queue_card(panel: Panel, episode_id: str, summary: dict,
                standing: Standing) -> str:
    """One row of the queue, with its action on it.

    The action is here rather than behind a click: the whole point of the
    queue is that opening it and acting are the same gesture.
    """
    t = panel.token
    tone = {"failed": "bad", "blocked": "bad",
            "ready to publish": "ok"}.get(standing.label, "open")
    length = (f' &middot; {summary["duration_sec"]}s'
              if summary["duration_sec"] else "")

    action = ""
    if standing.label == "script ready":
        action = _post(f"/episode/{episode_id}/approve-1", t,
                       "Approve the script", cls="small")
    elif standing.label == "video ready":
        action = _post(f"/episode/{episode_id}/approve-2", t,
                       "Approve for publishing", cls="small")
    elif standing.label == "ready to publish":
        action = _post(
            f"/episode/{episode_id}/publish", t, "Publish",
            fields='<input type="hidden" name="confirm" value="1">',
            cls="small danger",
            confirm="This uploads for real. Continue?")

    return f"""<div class="card"><div class="row">
  <div class="grow"><h3><a href="/episode/{episode_id}{_q(t)}">
    {_esc(summary["title"])}</a></h3>
    <span class="n">{episode_id} &middot; {_esc(summary["profile"])}{length}</span>
    <p class="n" style="margin:6px 0 0">{_esc(standing.detail)}</p></div>
  <div class="acts"><span class="tag {tone}">{_esc(standing.label)}</span>
    {action}</div></div></div>"""


def render_index(panel: Panel, flash: tuple[str, bool] | None) -> bytes:
    """The work queue.

    Grouped by state rather than listed by date, because the only
    question this page exists to answer is what needs a person now.
    Everything else is machinery reporting that it is fine.
    """
    import profiles

    from .recipes import available as available_recipes
    t = panel.token
    job = panel.job_snapshot()
    running_id = job["episode"] if job and job["running"] else None

    groups: dict[str, list] = {"needs_you": [], "running": [], "done": []}
    for episode_id in panel.episode_ids():
        summary = panel.summary(episode_id)
        standing = standing_of(summary, episode_id == running_id)
        groups[standing.group].append((standing.order, episode_id,
                                       summary, standing))
    for rows in groups.values():
        rows.sort(key=lambda r: (r[0], r[1]))

    cloud = render_cloud_episodes(panel)
    waiting = len(groups["needs_you"])
    parts = [
        '<div class="row"><div class="grow">'
        f'<h1>{waiting} waiting for you</h1>'
        '<p class="sub">Everything else is running or finished.</p></div>'
        f'<a class="btn ghost" href="/settings{_q(t)}">Settings</a> '
        f'<a class="btn ghost" href="/health{_q(t)}">System</a></div>'
    ]
    if flash:
        parts.append(_flash(flash))

    if not waiting:
        parts.append('<div class="card empty">Nothing needs a decision. '
                     'Make a new episode below, or come back later.</div>')
    for _, episode_id, summary, standing in groups["needs_you"]:
        parts.append(_queue_card(panel, episode_id, summary, standing))

    if groups["running"]:
        parts.append(f'<h2>Working ({len(groups["running"])})</h2>')
        for _, episode_id, summary, standing in groups["running"]:
            spin = '<span class="spin"></span>' if episode_id == running_id else ""
            parts.append(f"""<div class="card"><div class="row">
  <div class="grow"><h3>{spin}<a href="/episode/{episode_id}{_q(t)}">
    {_esc(summary["title"])}</a></h3>
    <span class="n">{episode_id} &middot; {_esc(standing.detail)}</span></div>
  <span class="tag">{_esc(standing.label)}</span></div></div>""")

    # New episodes and finished ones are both secondary to the queue, so
    # they sit below it rather than competing with it.
    profile_opts = "".join(
        f'<option value="{_esc(p)}">{_esc(p)}</option>'
        for p in profiles.available() if not p.endswith("_shorts"))
    recipe_opts = "".join(f'<option value="{_esc(r)}">{_esc(r)}</option>'
                          for r in available_recipes())
    parts.append(f"""<h2>New episode</h2><div class="card">
<form method="post" action="/new">{_tok(t)}
  <div class="fields">
    <div><label for="profile">Profile</label>
      <select id="profile" name="profile">{profile_opts}</select></div>
    <div><label for="recipe">Recipe</label>
      <select id="recipe" name="recipe">{recipe_opts}</select></div>
    <div><label for="id">Id (optional)</label>
      <input id="id" type="text" name="id" placeholder="auto"></div>
    <button type="submit">Create</button>
  </div></form></div>""")

    if cloud:
        parts.append(cloud)

    if groups["done"]:
        rows = "".join(
            f'<p class="n"><a href="/episode/{eid}{_q(t)}">{_esc(sm["title"])}'
            f'</a> &middot; {eid}</p>'
            for _, eid, sm, _st in groups["done"])
        parts.append(f'<h2>Published ({len(groups["done"])})</h2>'
                     f'<div class="card">{rows}</div>')

    return _page("Net of Fees", "".join(parts))


def render_login(panel: Panel, message: str = "",
                 first_run: bool = False) -> bytes:
    """The sign-in page, or the one that sets a password the first time."""
    locked = panel.credentials.locked_for
    if locked:
        message = f"Too many attempts. Try again in {locked} seconds."
    title = "Set a password" if first_run else "Sign in"
    note = ("This panel can publish to your channel, so give it a real "
            f"password of at least {panel_auth.MIN_PASSWORD} characters."
            if first_run else "")
    second = ('<div><label for="again">Repeat it</label>'
              '<input id="again" type="password" name="again" '
              'autocomplete="new-password"></div>') if first_run else ""
    action = "/set-password" if first_run else "/login"
    body = f"""<h1>{title}</h1>
<p class="sub">{_esc(note) or "Net of Fees control panel"}</p>
{f'<div class="flash bad">{_esc(message)}</div>' if message else ""}
<div class="card"><form method="post" action="{action}">
  <div class="fields">
    <div><label for="password">Password</label>
      <input id="password" type="password" name="password"
             autocomplete="{'new-password' if first_run else 'current-password'}"
             autofocus></div>
    {second}
    <button type="submit">{title}</button>
  </div></form></div>"""
    return _page(title, body)


SETTING_GROUPS = (
    ("Alerts", "Mailed when something breaks. Nothing else is sent.",
     ("ALERT_EMAIL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
      "SMTP_APP_PASSWORD")),
    ("Schedule", "The number of slots is also the episodes released a day.",
     ("PUBLISH_SLOTS", "CHANNEL_TZ", "BATCH_SIZE", "MAX_QUEUE_DAYS")),
    ("Voice", "VOICE_SPEED above 1.4 starts to sound hurried.",
     ("VOICE", "VOICE_ENGINE", "VOICE_SPEED")),
    ("Instagram", "Meta requires an HTTPS redirect, so the token is pasted "
                  "rather than connected here.",
     ("IG_USER_ID", "IG_ACCESS_TOKEN")),
    ("Storage", "Optional. A cloud render\u2019s artifact is deleted after "
                "seven days; this keeps a copy that is not. R2 gives 10 GB "
                "free and bills past it without asking, so uploads stop at "
                "the cap rather than start charging.",
     ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID",
      "R2_SECRET_ACCESS_KEY", "R2_PUBLIC_BASE_URL",
      "R2_STORAGE_CAP_GB", "R2_RETAIN_DAYS")),
    ("Disk", "0 keeps everything. A video is only ever removed once it "
             "is published or archived \u2014 an unpublished one is the "
             "only copy of work you have not judged yet.",
     ("RETAIN_WORKING_DAYS", "RETAIN_VIDEO_DAYS")),
    ("Voice reference", "Where your recordings live if not on this machine. "
                        "This link is a credential: it can clone your voice.",
     ("VOICE_REFERENCE_URL",)),
    ("Pictures", "Each scene says what it should show; this decides who "
                 "draws it. Tried in order, so a generator being down "
                 "falls through to stock instead of a title card. A "
                 "Creative Commons picture is credited in the description "
                 "automatically. Set IMAGE_FETCH to 0 to place pictures "
                 "in sources/ yourself.",
     ("IMAGE_PROVIDERS", "IMAGE_FETCH", "IMAGE_STYLE", "IMAGE_LOCAL_URL")),
    ("Reusing pictures", "A picture drawn once is kept and used again "
                         "when a later scene asks for something close. "
                         "IMAGE_MAX_NEW caps how many an episode draws \u2014 "
                         "set it to 6 and a fourteen-scene episode draws "
                         "six and reuses them, which is faster and often "
                         "looks better than fourteen near-identical ones. "
                         "0 means no cap.",
     ("IMAGE_MAX_NEW", "IMAGE_REUSE", "IMAGE_REUSE_THRESHOLD")),
    ("Content", "Defaults for new episodes and the trend scan.",
     ("PROFILE", "SCRIPT_ENGINE", "GEMINI_API_KEY", "YOUTUBE_API_KEY",
      "TREND_REGION")),
)


def render_cloud_episodes(panel: Panel) -> str:
    """Episodes that exist in storage but not on this machine.

    The queue reads the local episodes directory, so when the rendering
    happens on a runner this panel shows nothing at all. The work is not
    lost -- it is in the bucket -- but a review panel that cannot see
    what it is meant to review is a panel that gets closed. Fetching one
    brings it here to be watched and judged like any other.
    """
    from . import archive

    target = archive.Target()
    if not target.configured:
        return ''
    try:
        rows = archive.archived_episodes(target)
    except Exception as exc:                 # storage down is not fatal
        return (f'<div class="card"><h3>In storage</h3>'
                f'<p class="n">Could not read the bucket: {_esc(str(exc))}'
                '</p></div>')
    here = set(panel.episode_ids())
    missing = [r for r in rows if r["id"] not in here]
    if not missing:
        return ''
    t = panel.token
    busy = bool((panel.job_snapshot() or {}).get("running"))
    lines = []
    for row in missing[:12]:
        size = f'{row["bytes"] / 1e6:.0f} MB'
        what = "video and record" if row["video"] else "record only"
        fetch = _post("/fetch-episode", t, "Fetch",
                      fields=f'<input type="hidden" name="episode" '
                             f'value="{_esc(row["id"])}">',
                      cls="ghost", disabled=busy)
        lines.append(f'<p class="n"><span class="tag open">'
                     f'{_esc(row["id"])}</span> {what}, {size} '
                     f'{fetch}</p>')
    return ('<div class="card"><h3>In storage</h3>'
            '<p class="n">Rendered elsewhere and not on this machine '
            'yet. Fetching one puts it in the queue to review.</p>'
            + "".join(lines) + '</div>')


def connect_outcome(panel: Panel) -> str:
    """What the last connect attempt did.

    A connect job is not an episode, so its output appeared on no page:
    you pressed the button, read "Started", and never learned whether a
    consent screen opened or the client secret was missing. The queue
    shows a render's log because a render has an episode to show it on;
    this is the same courtesy for the one job that has not.
    """
    job = panel.job_snapshot()
    if not job or not job["label"].startswith("connect"):
        return ""
    if job["running"]:
        return ('<p class="n">Connecting&hellip; a browser tab should have '
                'opened. Finish the consent screen there.</p>')
    lines = [line for line in job["lines"] if line.strip()][-8:]
    tone = "ok" if job["returncode"] == 0 else "bad"
    head = ("Connected." if job["returncode"] == 0
            else "That did not connect.")
    body = _esc("\n".join(lines)) if lines else ""
    return (f'<p class="n"><span class="tag {tone}">{head}</span></p>'
            + (f'<pre class="log">{body}</pre>' if body else ""))


def render_settings(panel: Panel, flash: tuple[str, bool] | None) -> bytes:
    """Credentials and defaults.

    A secret is never rendered back -- a field carrying its own value is
    one that leaks to a screenshot or a password manager. Each shows only
    whether it is set, and leaving it blank keeps what is there.
    """
    t = panel.token
    current = settings_store.describe()
    try:
        from . import channels as channels_mod
        channels = {k: channels_mod.describe(k) for k in channels_mod.known()}
    except Exception:
        channels = {}

    parts = [f'<p><a href="/{_q(t)}">&larr; queue</a></p>',
             "<h1>Settings</h1>",
             '<p class="sub">Stored in .env on this machine. Secrets are '
             'never shown back; leave a field blank to keep it.</p>']
    if flash:
        parts.append(_flash(flash))

    disk = prune.usage(panel.episodes_dir)
    parts.append(
        f'<div class="card"><h3>Disk</h3><p class="n">'
        f'{disk["episodes"]} episodes using {disk["mb"]} MB. '
        f'Working files are reproducible; a video is not until it is '
        f'published or archived.</p></div>')

    rows = []
    for key, info in channels.items():
        ok = "(present)" in info["youtube"]
        tone = "ok" if ok else "open"
        rows.append(f'<p class="n"><span class="tag {tone}">{_esc(key)}'
                    f'</span> {_esc(info["youtube"])}</p>')
    connect = _post("/connect-youtube", t, "Connect a YouTube channel",
                    cls="ghost")
    empty = '<p class="n">No channels configured.</p>'
    parts.append(
        '<div class="card"><h3>YouTube</h3>' + ("".join(rows) or empty)
        + '<p class="n">Opens Google&rsquo;s consent screen in a browser '
          'on this machine. Needs secrets/client_secret.json from the '
          'Cloud console.</p>'
        + f'<div class="acts" style="margin-top:10px">{connect}</div>'
        + connect_outcome(panel) + '</div>')

    form_parts = []
    for title, note, names in SETTING_GROUPS:
        fields = []
        for name in names:
            info = current.get(name, {})
            if info.get("secret"):
                state = "set" if info.get("set") else "not set"
                tone = "ok" if info.get("set") else ""
                fields.append(
                    f'<div><label for="{name}">{name} '
                    f'<span class="tag {tone}">{state}</span></label>'
                    f'<input id="{name}" type="password" name="{name}" '
                    f'placeholder="unchanged" autocomplete="new-password">'
                    f'</div>')
            else:
                fields.append(
                    f'<div><label for="{name}">{name}</label>'
                    f'<input id="{name}" type="text" name="{name}" '
                    f'value="{_esc(info.get("value", ""))}"></div>')
        form_parts.append(f'<div class="card"><h3>{_esc(title)}</h3>'
                          f'<p class="n">{_esc(note)}</p>'
                          f'<div class="fields">{"".join(fields)}</div></div>')
    parts.append(
        f'<form method="post" action="/settings">{_tok(t)}'
        + "".join(form_parts)
        + '<div class="acts"><button type="submit">Save</button>'
        + _post("/notify-test", t, "Send a test alert", cls="ghost")
        + _post("/storage-test", t, "Test storage", cls="ghost")
        + _post("/prune", t, "Preview a cleanup",
                fields='<input type="hidden" name="dry" value="1">',
                cls="ghost")
        + "</div></form>")

    if panel.credentials.is_set:
        parts.append(
            '<h2>Panel password</h2><div class="card">'
            f'<form method="post" action="/set-password">{_tok(t)}'
            '<div class="fields">'
            '<div><label for="np">New password</label>'
            '<input id="np" type="password" name="password" '
            'autocomplete="new-password"></div>'
            '<div><label for="na">Repeat it</label>'
            '<input id="na" type="password" name="again" '
            'autocomplete="new-password"></div>'
            '<button class="ghost" type="submit">Change</button>'
            '</div></form>'
            f'<div class="acts" style="margin-top:12px">'
            f'<form method="post" action="/logout">{_tok(t)}'
            '<button class="ghost" type="submit">Sign out</button>'
            '</form></div></div>')
    return _page("Settings - Net of Fees", "".join(parts))


def render_health(panel: Panel, flash: tuple[str, bool] | None) -> bytes:
    t = panel.token
    info = health()
    rows = "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>"
                   for k, v in info.items())
    parts = [f'<p><a href="/{_q(t)}">&larr; episodes</a></p>',
             "<h1>System</h1>",
             '<p class="sub">What this machine can do right now.</p>']
    if flash:
        parts.append(_flash(flash))
    parts.append(f'<div class="card"><dl class="kv">{rows}</dl></div>')
    parts.append('<div class="card"><h3>Alert channel</h3>'
                 '<p class="n">Sends a real test message.</p>'
                 f'<div class="acts">{_post("/notify-test", t, "Send a test alert", cls="ghost")}</div></div>')
    return _page("System - Net of Fees", "".join(parts))


def _scene_block(ep: Episode, m: Manifest, episode_id: str,
                 token: str | None, locked: bool) -> str:
    out = []
    for scene in m.scenes:
        chart = ep.dir / "visuals" / f"{scene.slug}.png"
        img = (f'<img src="/asset/{episode_id}/visuals/{scene.slug}.png'
               f'{_q(token)}" alt="scene {scene.index} chart">'
               if chart.exists() else "")
        wav = ep.audio_dir / f"{scene.slug}.wav"
        audio = (f'<audio controls preload="none" '
                 f'src="/asset/{episode_id}/audio/{scene.slug}.wav{_q(token)}">'
                 f'</audio>' if wav.exists() else "")
        secs = (f"{scene.duration_sec:.2f}s" if scene.duration_sec
                else "not measured")
        if locked:
            toggle = ""
        else:
            toggle = _post(
                f"/episode/{episode_id}/flag", token,
                "Clear flag" if scene.flagged else "Flag this scene",
                fields=f'<input type="hidden" name="index" value="{scene.index}">'
                       f'<input type="hidden" name="on" value='
                       f'"{"0" if scene.flagged else "1"}">',
                cls="ghost small")
        note = (' <span class="tag bad">flagged</span>'
                if scene.flagged else "")
        out.append(f"""<div class="scene{' flagged' if scene.flagged else ''}">
  {img}<div class="grow">
    <span class="n">scene {scene.index} &middot; {secs}</span>{note}
    <p>{_esc(scene.text)}</p>{audio}
    <div class="acts" style="margin-top:8px">{toggle}</div></div></div>""")
    return "".join(out) or '<p class="empty">No scenes yet.</p>'


def render_episode(panel: Panel, episode_id: str,
                   flash: tuple[str, bool] | None) -> bytes:
    ep, m = panel.load(episode_id)
    s = panel.summary(episode_id)
    t = panel.token
    job = panel.job_snapshot()
    job_here = job if job and job["episode"] == episode_id else None

    parts = [f'<p><a href="/{_q(t)}">&larr; episodes</a></p>',
             f"<h1>{_esc(s['title'])}</h1>",
             f'<p class="sub">{episode_id} &middot; {_esc(s["profile"])}'
             f' &middot; {_esc(s["recipe"])} &middot; tier {_esc(s["tier"])}'
             f' &middot; voice {_esc(s["voice"])}</p>']
    if flash:
        parts.append(_flash(flash))

    # ---- pipeline controls
    pills = "".join(f'<span class="pill {s["stages"][st]}">{st}</span>'
                    for st in STAGES)
    stage_opts = "".join(f'<option value="{st}">{st}</option>' for st in STAGES)
    running = bool(job and job["running"])
    busy = ' disabled title="a job is already running"' if running else ""
    parts.append(f"""<div class="card"><h3>Pipeline</h3>
<div class="pills">{pills}</div>
<div class="acts" style="margin-top:14px">
  <form method="post" action="/episode/{episode_id}/run">{_tok(t)}
    <button type="submit"{busy}>Run to the next gate</button></form>
  <form method="post" action="/episode/{episode_id}/run">{_tok(t)}
    <input type="hidden" name="force" value="1">
    <select name="stage" aria-label="stage">{stage_opts}</select>
    <button class="ghost" type="submit"{busy}>Re-run this stage</button></form>
  {_post(f"/episode/{episode_id}/cancel", t, "Cancel", cls="ghost")
   if running else ""}
</div></div>""")

    if job_here:
        parts.append(f"""<div class="card"><h3 id="jobmeta">
  {'<span class="spin"></span>' if job_here["running"] else ''}
  {_esc(job_here["label"])}</h3>
<pre class="log" id="log">{_esc(chr(10).join(job_here["lines"]))}</pre></div>""")

    # ---- gate 1
    parts.append("<h2>Gate 1 &mdash; the script</h2>")
    findings = s["scrub_findings"]
    scan = ('<span class="tag ok">secret scan clean</span>' if not findings
            else f'<span class="tag bad">{findings} scrub findings</span>')
    parts.append(f'<div class="card"><div class="row"><div class="grow">'
                 f'<h3>Scenes</h3></div>{scan}</div>'
                 f'{_scene_block(ep, m, episode_id, t, s["gate_1"])}</div>')
    parts.append(_gate_form(episode_id, 1, s["gate_1"], s["gate_1_blockers"],
                            t, "Approve the script"))

    # ---- gate 2
    parts.append("<h2>Gate 2 &mdash; the video</h2>")
    if ep.video.exists():
        mb = ep.video.stat().st_size / 1_048_576
        drift = (f' &middot; drift {s["drift_sec"] * 1000:.0f}ms'
                 if s["drift_sec"] is not None else "")
        parts.append(f"""<div class="card">
  <video class="main" controls preload="metadata"
         src="/asset/{episode_id}/out/video.mp4{_q(t)}"></video>
  <p class="n">{mb:.1f} MB &middot; {s["duration_sec"]}s{drift}</p></div>""")
    else:
        parts.append('<div class="card empty">Not rendered yet.</div>')

    thumbs = [n for n in range(1, 6) if ep.thumbnail(n).exists()]
    if thumbs:
        buttons = []
        for n in thumbs:
            picked = s["chosen_thumbnail"] == n
            buttons.append(f"""<form method="post"
  action="/episode/{episode_id}/thumbnail">{_tok(t)}
  <input type="hidden" name="n" value="{n}">
  <button class="thumb{' picked' if picked else ''}" type="submit">
    <img src="/asset/{episode_id}/out/thumb_{n}.png{_q(t)}"
         alt="thumbnail {n}">
    <span>{'chosen' if picked else f'option {n}'}</span></button></form>""")
        note = ("" if s["chosen_thumbnail"] else '<p class="n">Pick one, or '
                'YouTube auto-generates a frame.</p>')
        parts.append(f'<div class="card"><h3>Thumbnail</h3>{note}'
                     f'<div class="thumbs">{"".join(buttons)}</div></div>')

    meta_path = ep.dir / "metadata.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        parts.append(_title_block(episode_id, meta, t, s["gate_2"]))
        yt, ig = meta.get("youtube", {}), meta.get("instagram", {})
        tags = ", ".join(yt.get("tags", []))
        parts.append(f"""<div class="card"><h3>What gets posted</h3>
  <p class="n">YouTube description</p>
  <pre>{_esc(yt.get("description", ""))}</pre>
  <p class="n" style="margin-top:14px">Tags</p><pre>{_esc(tags)}</pre>
  <p class="n" style="margin-top:14px">Instagram caption</p>
  <pre>{_esc(ig.get("caption", ""))}</pre></div>""")

    reels = sorted(ep.shorts_dir.glob("short_*.mp4")) \
        if ep.shorts_dir.exists() else []
    if reels:
        cards = "".join(
            f"""<div class="reel"><video controls preload="none"
      src="/asset/{episode_id}/out/shorts/{r.name}{_q(t)}"></video>
      <span class="n">{_esc(r.stem.replace("short_", "cut "))} &middot;
      {r.stat().st_size / 1_048_576:.1f} MB</span></div>""" for r in reels)
        parts.append(f'<div class="card"><h3>Vertical cuts</h3>'
                     f'<p class="n">The Reel posts cut 1 unless you pass '
                     f'--reel.</p><div class="reels">{cards}</div></div>')

    if ep.captions.exists():
        srt = ep.captions.read_text(encoding="utf-8", errors="replace")
        parts.append(f'<div class="card"><h3>Captions</h3>'
                     f'<pre style="max-height:240px;overflow:auto">'
                     f'{_esc(srt)}</pre></div>')

    parts.append(_gate_form(episode_id, 2, s["gate_2"], s["gate_2_blockers"],
                            t, "Approve for publishing"))

    # ---- publishing
    parts.append("<h2>Publishing</h2>")
    parts.append(_publish_block(episode_id, s, t, running))
    return _page(f"{s['title']} - Net of Fees", "".join(parts),
                 poll_url=(f"/api/job{_q(t)}" if job_here
                           and job_here["running"] else None))


def _title_block(episode_id: str, meta: dict, token: str | None,
                 locked: bool) -> str:
    chosen = meta.get("chosen_title", "")
    rows = []
    for c in meta.get("title_candidates", []):
        text, warn = c.get("text", ""), c.get("warnings", [])
        picked = text == chosen
        flag = (f' <span class="tag bad">{_esc("; ".join(warn))}</span>'
                if warn else "")
        button = ("" if locked or picked else _post(
            f"/episode/{episode_id}/title", token, "Use this",
            fields=f'<input type="hidden" name="text" value="{_esc(text)}">',
            cls="ghost small"))
        mark = '<span class="tag ok">in use</span>' if picked else ""
        rows.append(f"""<div class="scene"><div class="grow">
  <p style="margin:0 0 4px"><strong>{_esc(text)}</strong>{flag}</p>
  <span class="n">{_esc(c.get("why", ""))} &middot;
    {_esc(c.get("length", 0))} chars</span></div>
  <div class="acts">{mark}{button}</div></div>""")
    return (f'<div class="card"><h3>Title</h3>'
            f'<p class="n">Only generated candidates can be chosen: they are '
            f'the ones checked for advisory language.</p>{"".join(rows)}</div>')


def _gate_form(episode_id: str, which: int, passed: bool,
               blockers: list[str], token: str | None, label: str) -> str:
    if passed:
        return (f'<div class="card"><span class="tag ok">'
                f'gate {which} cleared</span></div>')
    if blockers:
        items = "".join(f"<li>{_esc(b)}</li>" for b in blockers)
        return (f'<div class="card"><button disabled>{label}</button>'
                f'<ul class="blockers">{items}</ul></div>')
    return (f'<div class="card">'
            f'{_post(f"/episode/{episode_id}/approve-{which}", token, label)}'
            f'</div>')


def _publish_block(episode_id: str, s: dict, token: str | None,
                   running: bool) -> str:
    posts = []
    for row in s["platform_posts"]:
        where = row.get("platform", "?")
        ident = row.get("external_id")
        link = ""
        if ident and where == "youtube":
            link = (f' &middot; <a href="https://youtu.be/{_esc(ident)}" '
                    f'target="_blank" rel="noreferrer">open</a>')
        elif ident and where == "instagram":
            link = f" &middot; {_esc(ident)}"
        cls = {"posted": "ok", "failed": "bad"}.get(row.get("status"), "open")
        posts.append(f'<p class="n"><span class="tag {cls}">{_esc(where)}'
                     f'</span> {_esc(row.get("status"))}{link}'
                     f'{" &middot; " + _esc(row["error"])[:120] if row.get("error") else ""}</p>')
    history = "".join(posts) or '<p class="n">Nothing published yet.</p>'

    modes = {
        "manual": "Wait for me to press publish",
        "on_approval": "Publish as soon as I approve it",
        "at_slot": "Publish at the next slot after I approve it",
    }
    current = s.get("publish_mode", "manual")
    options = "".join(
        f'<option value="{key}"{" selected" if key == current else ""}>'
        f'{label}</option>' for key, label in modes.items())
    chooser = f"""<div class="card"><h3>After approval</h3>
  <p class="n">Approving is always yours. This only decides whether a
  second press is needed afterwards.</p>
  <div class="acts" style="margin-bottom:12px">{_post(f"/episode/{episode_id}/keep", token,
        "Stop keeping this one" if s.get("keep_files")
        else "Keep this episode whole",
        fields=f'<input type="hidden" name="on" value="{"0" if s.get("keep_files") else "1"}">',
        cls="ghost small")}</div>
  <form method="post" action="/episode/{episode_id}/publish-mode">
    {_tok(token)}<div class="fields">
      <div><label for="mode">When approved</label>
        <select id="mode" name="mode">{options}</select></div>
      <button class="ghost" type="submit">Save</button>
    </div></form>{
      f'<p class="n">Scheduled for {_esc((s.get("publish_after") or "")[:16].replace("T", " "))}</p>'
      if s.get("publish_after") and current == "at_slot" else ""}</div>"""

    if not s["gate_2"]:
        return (f'{chooser}<div class="card">'
                f'<p class="empty">Clear gate 2 first.</p>{history}</div>')
    dry = _post(f"/episode/{episode_id}/publish", token,
                "Dry run (nothing leaves the machine)",
                fields='<input type="hidden" name="dry" value="1">',
                cls="ghost", disabled=running)
    both = _post(
        f"/episode/{episode_id}/publish", token, "Publish to both",
        fields='<input type="hidden" name="confirm" value="1">',
        cls="danger", disabled=running,
        confirm="This uploads to YouTube and posts a Reel for real. Continue?")
    yt = _post(
        f"/episode/{episode_id}/publish", token, "YouTube only",
        fields='<input type="hidden" name="confirm" value="1">'
               '<input type="hidden" name="only" value="youtube">',
        cls="ghost", disabled=running,
        confirm="This uploads to YouTube for real. Continue?")
    ig = _post(
        f"/episode/{episode_id}/publish", token, "Reel only",
        fields='<input type="hidden" name="confirm" value="1">'
               '<input type="hidden" name="only" value="instagram">',
        cls="ghost", disabled=running,
        confirm="This posts a Reel for real. Continue?")
    return chooser + f"""<div class="card"><h3>Publish</h3>
<p class="n">Uploads are private on YouTube by default. Publishing cannot
be undone from here.</p>
<div class="acts" style="margin-top:12px">{dry}{yt}{ig}{both}</div>
<div style="margin-top:16px">{history}</div>
{'<p class="n">A job is running; wait for it.</p>' if running else ''}</div>"""


# ---- server -----------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    panel: Panel = None  # type: ignore[assignment]
    server_version = "net-of-fees"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        self.panel.log.debug(fmt % args)

    def _session(self) -> str | None:
        return panel_auth.session_from(self.headers.get("Cookie"))

    def _authorised(self, supplied: str | None) -> bool:
        """A valid session, or the token, or nothing required.

        Three ways in, deliberately: a person logs in, a script passes
        the token, and a panel with neither configured is one that only
        localhost can reach anyway.
        """
        if self.panel.sessions.valid(self._session()):
            return True
        if self.panel.credentials.is_set:
            return False      # a password is set, so a session is needed
        if self.panel.token is None:
            return True
        return bool(supplied) and secrets.compare_digest(
            supplied, self.panel.token)

    def _same_origin(self) -> bool:
        """A POST from another site must not approve or publish anything.

        A missing Origin is a same-origin form post from an older browser
        and is allowed; a mismatched one is not.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        if urllib.parse.urlparse(origin).netloc == host:
            return True
        # Logged because the refusal is indistinguishable from a bug
        # when it happens to you: the page looks right, the button does
        # nothing, and the message cannot name the mismatch without
        # telling an attacker what would have matched.
        get(None, "panel").warning(
            f"refused a form post: Origin {origin!r} is not Host {host!r}")
        return False

    def _send(self, body: bytes, code: int = 200,
              ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # This panel can publish; keep it out of caches and off other pages.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        # same-origin, not no-referrer. Under no-referrer a browser
        # strips the origin from form posts as well, sending
        # `Origin: null` -- which the cross-site check below then
        # refuses, so the header defeated the check it was next to and
        # every button returned 403. This still sends no referrer to
        # any other site, which was the point.
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self, message: str, code: int = 403) -> None:
        self._send(_page("Not allowed",
                         f"<h1>{code}</h1><p class=sub>{_esc(message)}</p>"
                         f'<p><a href="/{_q(self.panel.token)}">back</a></p>'),
                   code)

    def _back(self, episode_id: str, message: str, ok: bool) -> None:
        key = "ok" if ok else "err"
        target = f"/episode/{episode_id}" if episode_id != "new" else "/"
        self._redirect(f"{target}{_q(self.panel.token, **{key: message})}")

    # ---- routing ------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        flash = None
        if "ok" in query:
            flash = (query["ok"][0], True)
        elif "err" in query:
            flash = (query["err"][0], False)

        path = parsed.path.rstrip("/") or "/"

        if not self._authorised(query.get("token", [None])[0]):
            if self.panel.credentials.is_set:
                return self._send(render_login(self.panel), 401)
            if path == "/login":
                # No password yet: offer to set one rather than refuse.
                return self._send(render_login(self.panel, first_run=True))
            return self._deny("This panel needs the token from the link "
                              "printed when it started.")

        if path == "/login":
            return self._redirect("/")
        try:
            if path == "/":
                return self._send(render_index(self.panel, flash))
            if path == "/settings":
                return self._send(render_settings(self.panel, flash))
            if path == "/health":
                return self._send(render_health(self.panel, flash))
            if path == "/api/job":
                return self._send(
                    json.dumps(self.panel.job_snapshot()).encode(),
                    ctype="application/json")
            if path == "/api/episodes":
                data = [self.panel.summary(i)
                        for i in self.panel.episode_ids()]
                return self._send(json.dumps(data, indent=2).encode(),
                                  ctype="application/json")
            parts = path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "episode":
                return self._send(render_episode(self.panel, parts[1], flash))
            if len(parts) >= 3 and parts[0] == "asset":
                path_obj = self.panel.asset(
                    parts[1], urllib.parse.unquote("/".join(parts[2:])))
                ctype = (mimetypes.guess_type(path_obj.name)[0]
                         or "application/octet-stream")
                return self._send(path_obj.read_bytes(), ctype=ctype)
        except PipelineError as exc:
            return self._deny(str(exc), 404)
        self._deny("No such page.", 404)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8192:
            return self._deny("Unexpectedly large form.", 413)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(
            self.rfile.read(length).decode("utf-8", "replace")).items()}

        if not self._same_origin():
            return self._deny("Cross-site form posts are refused.")

        path_now = parsed.path.strip("/")
        if path_now == "login":
            return self._do_login(form)
        if path_now == "set-password":
            return self._do_set_password(form)
        if path_now == "logout":
            self.panel.sessions.end(self._session())
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", panel_auth.clear_cookie())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        if not self._authorised(form.get("token")):
            return self._deny("This action needs the token.")

        path = parsed.path.strip("/")
        try:
            if path == "new":
                message = self.panel.create(
                    form.get("id", "").strip(),
                    form.get("profile", ""), form.get("recipe", ""))
                return self._back("new", message, True)
            if path == "notify-test":
                from . import notify
                ok, detail = notify.test_channel()
                return self._redirect(
                    f"/health{_q(self.panel.token, **{('ok' if ok else 'err'): detail})}")
            if path == "settings":
                changed = settings_store.write(form)
                note = (f"Saved: {', '.join(changed)}." if changed
                        else "Nothing changed.")
                return self._redirect(
                    f"/settings{_q(self.panel.token, ok=note)}")
            if path == "storage-test":
                ok, detail = archive.test_target()
                key = "ok" if ok else "err"
                return self._redirect(
                    f"/settings{_q(self.panel.token, **{key: detail})}")
            if path == "prune":
                dry = form.get("dry") == "1"
                result = prune.sweep(self.panel.episodes_dir, dry_run=dry)
                if not result["enabled"]:
                    note = result["note"]
                else:
                    verb = "would free" if dry else "freed"
                    note = (f"{verb} {result['freed_mb']} MB from "
                            f"{len(result['episodes'])} episodes")
                    if result["kept"]:
                        note += f"; {len(result['kept'])} kept"
                return self._redirect(
                    f"/settings{_q(self.panel.token, ok=note)}")
            if path == "fetch-episode":
                return self._fetch_episode(form)
            if path == "connect-youtube":
                return self._connect_youtube(form)

            parts = path.split("/")
            if len(parts) != 3 or parts[0] != "episode":
                return self._deny("No such action.", 404)
            episode_id, action = parts[1], parts[2]
            message = self._act(episode_id, action, form)
        except (GateError, PipelineError, ValueError) as exc:
            self.panel.log.info(f"{path} refused: {exc}")
            return self._back(path.split("/")[1] if "/" in path else "new",
                              str(exc), False)
        self.panel.log.info(message)
        self._back(episode_id, message, True)

    def _do_login(self, form: dict):
        if not self.panel.credentials.is_set:
            return self._send(render_login(self.panel, first_run=True))
        if not self.panel.credentials.check(form.get("password", "")):
            locked = self.panel.credentials.locked_for
            message = ("Too many attempts." if locked
                       else "That password is not right.")
            return self._send(render_login(self.panel, message), 401)
        token = self.panel.sessions.start()
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", panel_auth.cookie_header(token))
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    def _do_set_password(self, form: dict):
        """Only available while no password exists.

        Changing one later goes through the settings page, which needs a
        session -- otherwise anyone who reached the panel could replace
        the password rather than guess it.
        """
        if self.panel.credentials.is_set and not self.panel.sessions.valid(
                self._session()):
            return self._deny("A password is already set. Sign in first.")
        password = form.get("password", "")
        if password != form.get("again", password):
            return self._send(render_login(
                self.panel, "Those did not match.", first_run=True), 400)
        try:
            self.panel.credentials.set_password(password)
        except ValueError as exc:
            return self._send(render_login(
                self.panel, str(exc), first_run=True), 400)
        token = self.panel.sessions.start()
        self.panel.log.info("panel password set")
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", panel_auth.cookie_header(token))
        self.send_header("Content-Length", "0")
        self.end_headers()
        return None

    def _fetch_episode(self, form: dict):
        """Bring one archived episode here so it can be reviewed.

        Run as a job rather than inline: a video is tens of megabytes
        and a request that blocks on it looks like a hung panel.
        """
        episode_id = (form.get("episode") or "").strip()
        if not EPISODE_ID.match(episode_id):
            return self._redirect(
                f"/{_q(self.panel.token, err='that is not an episode id')}")
        try:
            note = self.panel.start_job(
                episode_id, ["archive", "--restore", episode_id],
                f"fetch {episode_id}")
        except PipelineError as exc:
            note = str(exc)
        return self._redirect(f"/{_q(self.panel.token, ok=note)}")

    def _connect_youtube(self, form: dict):
        """Run Google's loopback OAuth for a channel.

        Google permits a localhost redirect for installed apps, which is
        what tools/youtube_auth.py already uses. Meta does not, which is
        why Instagram pastes a token instead of connecting here.
        """
        channel = (form.get("channel") or "").strip().lower() or None
        argv = ["--channel", channel] if channel else []
        try:
            note = self.panel.start_job(
                channel or "youtube", ["connect", *argv],
                "connect YouTube")
        except PipelineError as exc:
            note = str(exc)
        return self._redirect(f"/settings{_q(self.panel.token, ok=note)}")

    def _act(self, episode_id: str, action: str, form: dict) -> str:
        panel = self.panel
        if action == "approve-1":
            return panel.approve(episode_id, 1)
        if action == "approve-2":
            message = panel.approve(episode_id, 2)
            started = panel.drain_auto_publish()
            return f"{message} {started}" if started else message
        if action == "keep":
            return panel.set_keep(episode_id, form.get("on") == "1")
        if action == "publish-mode":
            return panel.set_publish_mode(episode_id,
                                          form.get("mode", "manual"))
        if action == "thumbnail":
            return panel.choose_thumbnail(episode_id, int(form.get("n", "0")))
        if action == "title":
            return panel.choose_title(episode_id, form.get("text", ""))
        if action == "flag":
            return panel.set_flag(episode_id, int(form.get("index", "0")),
                                  form.get("on") == "1")
        if action == "cancel":
            return panel.cancel_job()
        if action == "run":
            stage = form.get("stage")
            argv = [episode_id]
            if stage:
                if stage not in STAGES:
                    raise PipelineError(f"no stage {stage!r}")
                argv += ["--stage", stage]
                if form.get("force"):
                    argv.append("--force")
                label = f"re-run {stage}"
            else:
                label = "run to the next gate"
            return panel.start_job(episode_id, argv, label)
        if action == "publish":
            return self._publish(episode_id, form)
        raise PipelineError(f"no action {action!r}")

    def _publish(self, episode_id: str, form: dict) -> str:
        """Publishing is the one irreversible action, so a real post needs
        an explicit confirm field that the dry run does not."""
        dry = form.get("dry") == "1"
        if not dry and form.get("confirm") != "1":
            raise PipelineError("publishing for real needs a confirmation")
        argv = [episode_id, "--publish"]
        only = form.get("only")
        if only == "youtube":
            argv.append("--no-instagram")
        elif only == "instagram":
            argv.append("--instagram")
        if dry:
            argv.append("--dry-run")
        where = {"youtube": "YouTube", "instagram": "Instagram"}.get(
            only, "both platforms")
        return self.panel.start_job(
            episode_id, argv,
            f"{'dry run' if dry else 'publish'} to {where}")


def tailnet_address() -> str | None:
    """This machine's Tailscale address, if it is on a tailnet.

    Binding to it specifically is the point: 0.0.0.0 would also expose
    the panel to whatever wifi the laptop is on, and a cafe network is
    not a place to serve something that can publish to your channel.
    A tailnet address is reachable only by your own devices.
    """
    import socket

    # Tailscale hands out 100.64.0.0/10 (CGNAT space), which is what
    # makes the address recognisable without asking the daemon.
    try:
        import subprocess
        proc = subprocess.run(["tailscale", "ip", "-4"],
                              capture_output=True, text=True, timeout=10,
                              check=False)
        candidate = proc.stdout.strip().splitlines()
        if candidate and candidate[0].startswith("100."):
            return candidate[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass

    # No CLI, so look for the interface directly.
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            address = info[4][0]
            if address.startswith("100."):
                return address
    except socket.gaierror:
        pass
    return None


def build(host: str, port: int, token: str | None,
          episodes_dir: Path | None = None) -> tuple[ThreadingHTTPServer, str]:
    """Create the server. Returns it with the URL to open.

    Refuses to bind beyond this machine without a token, because the panel
    can publish to a real channel.
    """
    # A tailnet address is private to your own devices, but the panel
    # can still publish, so it is protected like any other non-local
    # bind rather than treated as safe because the network is.
    if host not in LOCAL_HOSTS and not token and not \
            panel_auth.Credentials().is_set:
        raise PipelineError(
            f"refusing to serve on {host} without a token: this panel can "
            "publish to your channel. Pass --token to get an unguessable "
            "link, or leave --host at 127.0.0.1."
        )
    panel = Panel(episodes_dir or config.EPISODES_DIR, token)
    handler = type("BoundHandler", (Handler,), {"panel": panel})
    server = ThreadingHTTPServer((host, port), handler)
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    return server, f"http://{shown}:{server.server_address[1]}/{_q(token)}"


def serve(host: str = "127.0.0.1", port: int = 8765,
          token: str | None = None) -> None:
    server, url = build(host, port, token)
    print(f"\n  Control panel: {url}")
    if token:
        print("  That link carries the token. Treat it like a password.")
    else:
        print("  Bound to this machine only; nothing else can reach it.")
    print("\n  Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped\n")
    finally:
        server.server_close()


def new_token() -> str:
    return secrets.token_urlsafe(24)


def token_from_env() -> str | None:
    return os.getenv("DASHBOARD_TOKEN") or None
