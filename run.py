#!/usr/bin/env python3
"""Net of Fees -- pipeline CLI.

The CLI is a complete system on its own. The backend and console, when
they exist, are convenience over it -- architecture doc, section 7.

    run.py new --recipe fee_drag        create an episode and export its data
    run.py 001                          run every stage, stopping at the next gate
    run.py 001 --from charts            resume from a stage
    run.py 001 --stage assemble --force re-run one stage
    run.py 001 --approve-script         clear Gate 1
    run.py 001 --approve                clear Gate 2
    run.py 001 --publish                run the upload stage
    run.py 001 --status                 show where the episode is
    run.py doctor                       check the environment

There is deliberately no flag that skips a gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import profiles
from pipeline import channels, config, dashboard, gates, logging_setup, notify, recovery, trends
from pipeline.config import spec_for
from pipeline.errors import ConfigError, PipelineError
from pipeline.manifest import PUBLISH_STAGES, STAGES, Episode, Manifest
from pipeline.recipes import available as available_recipes
from pipeline.stages import (
    assemble,
    captions,
    charts,
    export,
    finish,
    images,
    instagram,
    metadata,
    script_gen,
    shorts,
    thumbnail,
    upload,
    voice,
)

# An episode id becomes a directory name, and since a scheduled run can
# supply one from a workflow input, it is validated rather than trusted.
EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _episode(episode_id: str) -> Episode:
    if not EPISODE_ID.match(episode_id):
        raise ConfigError(
            f"bad episode id {episode_id!r}: letters, digits, dash and "
            "underscore only, up to 32 characters"
        )
    return Episode(episode_id, config.EPISODES_DIR)


def _episode_ids() -> list[str]:
    """Every episode on disk."""
    if not config.EPISODES_DIR.exists():
        return []
    return [d.name for d in config.EPISODES_DIR.iterdir()
            if d.is_dir() and EPISODE_ID.match(d.name)
            and (d / "manifest.json").exists()]


def _next_id() -> str:
    config.EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    existing = [d.name for d in config.EPISODES_DIR.iterdir() if d.is_dir()]
    numbers = [int(n) for n in existing if n.isdigit()]
    return f"{max(numbers, default=0) + 1:03d}"


# ---- stage dispatch ---------------------------------------------------


def _run_stage(name: str, ep: Episode, m: Manifest, settings, args) -> None:
    profile = profiles.load(m.profile)
    # The frame comes from the profile. Every render stage needs it, and
    # letting each default to LONGFORM is what made vertical episodes
    # unrunnable from the CLI.
    spec = spec_for(profile)
    if name == "export":
        params = json.loads(args.params) if args.params else {}
        export.run(ep, m, params)
    elif name == "script":
        script_gen.run(ep, m, engine=args.script_engine, profile=profile)
    elif name == "scrub":
        from pipeline.scrub import assert_clean
        assert_clean([ep.snapshot, ep.script])
        m.mark_done("scrub", findings=0)
    elif name == "voice":
        if getattr(args, "voice", None):
            m.voice = args.voice
        voice.run(ep, m, engine_name=args.voice_engine)
    elif name == "images":
        wanted = (getattr(args, "redraw", None) or "").strip()
        if wanted:
            try:
                scenes = [int(n) for n in wanted.replace(" ", "").split(",")
                          if n]
            except ValueError:
                raise ConfigError(
                    f"--redraw takes scene numbers, e.g. 3,14 (got {wanted!r})"
                ) from None
            images.redraw(ep, m, scenes)
        try:
            images.run(ep, m, profile,
                       force=getattr(args, "refetch_images", False))
        finally:
            if wanted:
                # Whatever happened, a scene whose replacement never
                # arrived keeps the picture it had.
                images.restore_set_aside(ep)
    elif name == "charts":
        charts.run(ep, m, profile, spec)
    elif name == "captions":
        captions.run(ep, m, method=args.captions)
    elif name == "assemble":
        # A profile declaring MOTION = "kenburns" carries its frame with a
        # picture, so it takes the moving-stills path: clips with
        # overlaid captions rather than stills with captions baked in.
        if getattr(profile, "MOTION", None) == "kenburns":
            from pipeline.stages import motion_assemble
            motion_assemble.run(ep, m, spec,
                                burn_captions=not args.no_captions,
                                profile=profile, music=not args.no_music,
                                ambient=not args.no_ambience)
        else:
            assemble.run(ep, m, spec, burn_captions=not args.no_captions,
                         profile=profile, animate=not args.no_animate,
                         dissolve=not args.no_dissolve,
                         music=not args.no_music)
        finish.run(ep, m, look_name=args.look)
        # Insurance, not a step: unconfigured does nothing, and a failed
        # upload is logged rather than raised. The video is on disk.
        from pipeline import archive
        archive.archive_episode(ep, m)
    elif name == "metadata":
        metadata.run(ep, m, profile)
    elif name == "thumbnail":
        thumbnail.run(ep, m, profile)
    elif name == "shorts":
        shorts.run(ep, m, profile)
    elif name == "upload":
        upload.run(ep, m, dry_run=args.dry_run,
                   assume_not_posted=getattr(args, "assume_not_posted",
                                             False),
                   profile=profile,
                   privacy=getattr(args, "privacy", None))
    elif name == "instagram":
        instagram.run(ep, m, dry_run=args.dry_run, which=args.reel)
    else:
        raise PipelineError(f"no dispatch for stage {name!r}")


def _execute(ep: Episode, m: Manifest, name: str, settings, args, log) -> bool:
    """Run one stage. Returns False when a gate stops the run."""
    gate = m.gate_blocking(name)
    if gate:
        human = "--approve-script" if gate == "gate_1" else "--approve"
        log.info(f"stopped at {gate}: clear it with `run.py {m.id} {human}`")
        return False

    unmet = m.unmet_prerequisites(name)
    if unmet:
        raise PipelineError(
            f"cannot run {name!r}: these stages are not done: {unmet}"
        )

    # Asking for specific scenes to be drawn again is itself the reason
    # to run a stage that is already done. Without this, `--redraw 3,14`
    # moved two pictures aside, skipped the stage, and put them back --
    # a flag that reported success and changed nothing.
    asked_again = bool(getattr(args, "redraw", None)
                       or getattr(args, "refetch_images", False))
    if m.is_done(name) and not args.force and not (
            name == "images" and asked_again):
        log.debug(f"{name}: already done, skipping")
        return True

    try:
        # Network stages get retried on a transient failure; the render
        # stages are deterministic, so a retry would fail identically and
        # only cost a minute. recovery.classify decides what counts as
        # transient, and a gate failure never does.
        if recovery.worth_retrying(name) and not getattr(args, "no_retry", False):
            recovery.with_retry(
                lambda: _run_stage(name, ep, m, settings, args),
                what=f"{m.id}:{name}", episode_id=m.id)
        else:
            _run_stage(name, ep, m, settings, args)
    except PipelineError as exc:
        m.mark_failed(name, str(exc))
        ep.save_manifest(m)
        exc.log_path = str(ep.log)
        # Carry the location on the exception rather than mailing from here,
        # so there is exactly one place that sends an alert.
        exc.episode_id, exc.stage = m.id, name
        raise
    ep.save_manifest(m)
    # Success is what closes an open incident; nothing clears on a timer,
    # because a problem nobody fixed has not gone away.
    notify.recovered(m.id, name)
    return True


# ---- commands ---------------------------------------------------------


def cmd_new(args) -> int:
    episode_id = args.id or _next_id()
    ep = _episode(episode_id)
    if ep.manifest_path.exists() and not args.force:
        print(f"episode {episode_id} already exists; use --force to overwrite",
              file=sys.stderr)
        return 1
    settings = config.load()
    # Fail on a bad profile here rather than three stages later: loading it
    # is the only way to know the name is real.
    profiles.load(args.profile or settings.profile)
    ep.ensure_dirs()
    m = Manifest.new(
        episode_id,
        recipe=args.recipe,
        profile=args.profile or settings.profile,
        voice=args.voice or settings.voice,
        title=args.title or "",
    )
    logging_setup.configure(ep.log, args.verbose)
    log = logging_setup.get(episode_id, "new")

    params = json.loads(args.params) if args.params else {}
    export.run(ep, m, params)
    ep.save_manifest(m)
    log.info(f"created episode {episode_id} at {ep.dir}")
    print(f"\nEpisode {episode_id} created: {ep.dir}")
    print(f"Next:  python run.py {episode_id}")
    return 0


def cmd_run(args) -> int:
    ep = _episode(args.id)
    if not ep.manifest_path.exists():
        print(f"no episode {args.id}; create it with `run.py new`", file=sys.stderr)
        return 1
    m = ep.load_manifest()
    logging_setup.configure(ep.log, args.verbose)
    log = logging_setup.get(m.id, "run")
    settings = config.load()

    if args.approve_script:
        return _approve_gate_1(ep, m, log)
    if args.approve:
        return _approve_gate_2(ep, m, log)

    if args.stage:
        todo = [args.stage]
    elif args.publish:
        todo = ["instagram"] if args.instagram else list(PUBLISH_STAGES)
        if args.no_instagram:
            todo = [s for s in todo if s != "instagram"]
    else:
        start = STAGES.index(args.start) if args.start else 0
        todo = list(STAGES[start:])
        if not args.through_upload:
            todo = [s for s in todo if s not in PUBLISH_STAGES]

    for name in todo:
        if not _execute(ep, m, name, settings, args, log):
            break
    _print_status(m, ep)
    return 0


def _approve_gate_1(ep: Episode, m: Manifest, log) -> int:
    """Gate 1. Enforced in pipeline.gates, which the dashboard shares."""
    gates.approve_gate_1(ep, m)
    print(f"Gate 1 cleared. Next:  python run.py {m.id}")
    return 0


def _approve_gate_2(ep: Episode, m: Manifest, log) -> int:
    """Gate 2. The upload stage independently re-checks approved_at."""
    gates.approve_gate_2(ep, m)
    print(f"Gate 2 cleared. Next:  python run.py {m.id} --publish")
    print("           (add --no-instagram for YouTube only)")
    return 0


def _print_status(m: Manifest, ep: Episode) -> None:
    print(f"\n  Episode {m.id}  —  {m.title or '(untitled)'}")
    print(f"  profile={m.profile}  tier={m.tier}  voice={m.voice}")
    print()
    for name in STAGES:
        st = m.stage(name)
        if st.done:
            mark, detail = "done", ""
            if name == "assemble" and st.data.get("duration_sec"):
                detail = (f"  {st.data['duration_sec']}s, "
                          f"drift {st.data.get('drift_sec', 0)*1000:.0f}ms")
            elif st.data.get("count"):
                detail = f"  {st.data['count']} files"
            elif st.data.get("scenes"):
                detail = f"  {st.data['scenes']} scenes"
        elif st.error:
            mark, detail = "FAILED", f"  {st.error.splitlines()[0][:70]}"
        else:
            mark, detail = "-", ""
        gate = ""
        if name == "voice" and not m.gate_1_passed_at:
            gate = "   <- gate 1"
        if name == "shorts" and not m.gate_2_passed_at:
            gate = "   <- gate 2"
        print(f"    {name:<10} {mark:<7}{detail}{gate}")
    print()
    if ep.video.exists():
        print(f"  video: {ep.video}")


def cmd_status(args) -> int:
    ep = _episode(args.id)
    if not ep.manifest_path.exists():
        print(f"no episode {args.id}", file=sys.stderr)
        return 1
    _print_status(ep.load_manifest(), ep)
    return 0


def cmd_list(args) -> int:
    config.EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in sorted(config.EPISODES_DIR.iterdir()):
        if not (d / "manifest.json").exists():
            continue
        m = Manifest.load(d / "manifest.json")
        done = sum(1 for s in STAGES if m.is_done(s))
        rows.append((m.id, f"{done}/{len(STAGES)}", m.tier, m.title[:48]))
    if not rows:
        print("no episodes yet.  python run.py new --recipe fee_drag")
        return 0
    print(f"\n  {'id':<6}{'stages':<9}{'tier':<6}title")
    for r in rows:
        print(f"  {r[0]:<6}{r[1]:<9}{r[2]:<6}{r[3]}")
    print()
    return 0


def cmd_voices(args) -> int:
    """List voices: the stock ones available, and any configured locally."""
    from pipeline.voices.kokoro import DEFAULT_VOICE, available_voices, is_installed

    print("\n  Stock voices (Kokoro-82M, Apache 2.0, commercial use OK)")
    print(f"  kokoro installed: {'yes' if is_installed() else 'no - pip install -r requirements-voice.txt'}\n")
    for name, desc in available_voices().items():
        mark = " (default)" if name == DEFAULT_VOICE else ""
        print(f"    {name:<13}{desc}{mark}")

    print("\n  Configured locally")
    configured = sorted(d for d in config.VOICES_DIR.iterdir()
                        if d.is_dir()) if config.VOICES_DIR.exists() else []
    if not configured:
        print("    none")
    for d in configured:
        consent = "consent.md" if (d / "consent.md").exists() else "NO CONSENT"
        refs = len(list((d / "reference").glob("*"))) if (d / "reference").exists() else 0
        refs = max(0, refs - 1)  # discount .gitkeep
        print(f"    {d.name:<20} {consent:<12} {refs} reference file(s)")

    print("\n  Add a stock voice:  python tools/setup_stock_voice.py <voice>")
    print("  A voice is never cloned from a real person without their")
    print("  documented consent -- architecture doc, section 14.\n")
    return 0


def cmd_doctor(args) -> int:
    from pipeline.platform_support import describe_environment, ffmpeg_available
    env = describe_environment()
    print("\n  Environment\n")
    for k, v in env.items():
        print(f"    {k:<18} {v}")
    print(f"\n    recipes            {', '.join(available_recipes())}")
    print(f"    profiles           {', '.join(profiles.available())}")

    # Which account each content stream publishes to, and whether its
    # credentials are in place. Presence only, never a value.
    print("\n  Channels\n")
    for key in channels.known():
        info = channels.describe(key)
        print(f"    {info['channel']:<18} youtube: {info['youtube']}")
        print(f"    {'':<18} instagram: {info['instagram']}")

    problems = []
    if not ffmpeg_available():
        problems.append("FFmpeg or ffprobe not found -- see README setup")
    for mod in ("matplotlib", "PIL"):
        try:
            __import__(mod)
        except ImportError:
            problems.append(f"{mod} not installed -- pip install -r requirements.txt")
    print()
    if problems:
        for p in problems:
            print(f"    PROBLEM  {p}")
        print()
        return 1
    print("    all good\n")
    return 0


# ---- argument parsing -------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")

    new = sub.add_parser("new", help="create an episode and export its data")
    new.add_argument("--recipe", default="fee_drag")
    new.add_argument("--id")
    new.add_argument("--title")
    new.add_argument("--profile")
    new.add_argument("--voice")
    new.add_argument("--params", help="JSON passed to the recipe")
    new.add_argument("--force", action="store_true")
    new.set_defaults(func=cmd_new)

    sub.add_parser("list", help="list episodes").set_defaults(func=cmd_list)
    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sub.add_parser("notify-test",
                   help="send a test failure alert").set_defaults(func=cmd_notify_test)

    ar = sub.add_parser("archive", help="report or expire cloud storage")
    ar.add_argument("--prune", action="store_true",
                    help="delete objects past R2_RETAIN_DAYS")
    ar.add_argument("--dry-run", action="store_true")
    ar.add_argument("--backup", action="store_true",
                    help="store every episode's record and the picture "
                         "library; neither ever expires")
    ar.add_argument("--restore", metavar="ID",
                    help="bring one episode back from the bucket")
    ar.add_argument("--restore-library", action="store_true",
                    help="bring the reusable pictures back")
    ar.add_argument("-v", "--verbose", action="store_true")
    ar.set_defaults(func=cmd_archive)

    ck = sub.add_parser("check", help="inspect a rendered episode")
    ck.add_argument("id")
    ck.add_argument("-v", "--verbose", action="store_true")
    ck.set_defaults(func=cmd_check)

    pr = sub.add_parser("prune", help="free disk from finished episodes")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("-v", "--verbose", action="store_true")
    pr.set_defaults(func=cmd_prune)

    cn = sub.add_parser("connect", help="authorise a YouTube channel")
    cn.add_argument("--channel", help="for a second channel")
    cn.set_defaults(func=cmd_connect)

    ba = sub.add_parser("batch", help="draft several episodes at once")
    ba.add_argument("--count", type=int,
                    help=f"default {config.BATCH_SIZE} (BATCH_SIZE)")
    ba.add_argument("--profile")
    ba.add_argument("--recipe", default="fee_drag")
    ba.add_argument("--script-engine", default="template",
                    dest="script_engine", choices=script_gen.ENGINES)
    ba.add_argument("--publish-mode", default="at_slot",
                    choices=("manual", "on_approval", "at_slot"),
                    dest="publish_mode")
    ba.add_argument("--keep-going", action="store_true",
                    help="carry on past a failed episode")
    ba.add_argument("-v", "--verbose", action="store_true")
    ba.set_defaults(func=cmd_batch)

    pd = sub.add_parser("publish-due",
                        help="publish approved episodes whose slot arrived")
    pd.add_argument("--dry-run", action="store_true")
    pd.add_argument("--no-instagram", action="store_true")
    pd.add_argument("-v", "--verbose", action="store_true")
    pd.set_defaults(func=cmd_publish_due)

    he = sub.add_parser("health", help="what has been breaking")
    he.add_argument("--hours", type=int, default=24)
    he.add_argument("--mail", action="store_true",
                    help="mail the open list, if anything is open")
    he.set_defaults(func=cmd_health)

    tr = sub.add_parser("trends", help="what the sources are talking about")
    tr.add_argument("--profile", help="filter to this profile's subject words")
    tr.add_argument("--all", action="store_true",
                    help="do not filter by subject")
    tr.add_argument("--limit", type=int, default=10)
    tr.add_argument("--min-mentions", type=int, default=2,
                    dest="min_mentions")
    tr.add_argument("--json", help="also write the full report here")
    tr.add_argument("-v", "--verbose", action="store_true")
    tr.set_defaults(func=cmd_trends)

    dash = sub.add_parser("dashboard", help="serve the gate review panel")
    dash.add_argument("--host", default="127.0.0.1",
                      help="127.0.0.1 (default) needs no token; anything "
                           "else requires one")
    dash.add_argument("--port", type=int, default=8765)
    dash.add_argument("--tailscale", action="store_true",
                      help="serve on this machine's Tailscale address, so "
                           "your own devices can reach it and nothing else")
    dash.add_argument("--token", nargs="?", const="", default=None,
                      help="--token alone generates one; or pass a value")
    dash.add_argument("-v", "--verbose", action="store_true")
    dash.set_defaults(func=cmd_dashboard)
    sub.add_parser("voices", help="list available voices").set_defaults(func=cmd_voices)

    st = sub.add_parser("status", help="show one episode")
    st.add_argument("id")
    st.set_defaults(func=cmd_status)

    return p


def cmd_dashboard(args) -> int:
    """Serve the gate review panel."""
    logging_setup.configure(None, args.verbose)
    token = args.token or dashboard.token_from_env()
    if args.token == "":          # --token with no value: generate one
        token = dashboard.new_token()

    host = args.host
    if args.tailscale:
        host = dashboard.tailnet_address()
        if not host:
            print("\n  No Tailscale address found on this machine.")
            print("  Install it from https://tailscale.com/download, sign")
            print("  in, then run this again.\n")
            return 1
        from pipeline import panel_auth
        if not token and not panel_auth.Credentials().is_set:
            # A tailnet is private, but the panel can publish, so it is
            # not served unprotected on any address but this machine's.
            token = dashboard.new_token()
            print("\n  No panel password set, so a token was generated.")
            print("  Set a password from Settings to use a login instead.")
    dashboard.serve(host=host, port=args.port, token=token)
    return 0


def cmd_trends(args) -> int:
    """What the sources are talking about. Suggests subjects, nothing more."""
    logging_setup.configure(None, args.verbose)
    profile = profiles.load(args.profile or config.load().profile)
    keywords = () if args.all else trends.keywords_for(profile)
    report = trends.research(trends.default_sources(profile),
                             keywords=keywords,
                             min_mentions=args.min_mentions)

    print(f"\n  Read {report['signals_read']} signals, "
          f"{report['signals_fresh']} inside {report['window_hours']}h")
    print(f"  Sources: {', '.join(report['sources_used']) or 'none'}")
    for name, why in report["sources_failed"].items():
        print(f"    {name}: {why}")

    if report["topics"]:
        print("\n  Recurring across headlines\n")
        for topic in report["topics"][:args.limit]:
            print(f"    {topic['mentions']}x  {topic['term']}"
                  f"   [{', '.join(topic['sources'])}]")
            for ev in topic["evidence"][:2]:
                metric = next((f"{k}={v}" for k, v in ev.items()
                               if k in ("hn_score", "view_count")), "")
                print(f"         {ev['title'][:68]}  {metric}")

    if report["standouts"]:
        print("\n  Standing out in their own batch\n")
        for topic in report["standouts"][:args.limit]:
            ev = topic["evidence"][0]
            metric = next((f"{v}" for k, v in ev.items()
                           if k in ("hn_score", "view_count")), "")
            print(f"    {metric:>8}  {topic['term'][:66]}")
            print(f"              {ev['url'][:70]}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2),
                                   encoding="utf-8")
        print(f"\n  Written to {args.json}")
    print(f"\n  {report['method']}\n")
    return 0


def cmd_health(args) -> int:
    """What has been breaking, and what is still open."""
    from pipeline import health
    ledger = health.Ledger.load()
    stats = ledger.summary(args.hours)
    print(f"\n  Last {stats['window_hours']}h: {stats['runs']} stage runs, "
          f"{stats['failures']} failed\n")
    open_now = ledger.open_incidents()
    if not open_now:
        print("    nothing open\n")
    for incident in open_now:
        print("   ", incident.describe().replace("\n", "\n    "))
        fix = notify.remedy_for(incident.error_class, incident.message)
        if fix:
            print(f"      fix: {fix}")
        print()
    if args.mail:
        notify.open_issues(args.hours)
        print("  mailed the open list\n" if open_now
              else "  nothing open, so nothing mailed\n")
    return 1 if open_now else 0


def cmd_connect(args) -> int:
    """Authorise a YouTube channel through Google's loopback OAuth.

    The same flow tools/youtube_auth.py runs; here so the panel can
    start it. A browser opens on this machine, which is why it works
    locally and would not work from a hosted panel.
    """
    import subprocess
    import sys as _sys
    tool = Path(__file__).resolve().parent / "tools" / "youtube_auth.py"
    argv = ["--channel", args.channel] if args.channel else []
    proc = subprocess.run([_sys.executable, str(tool), *argv],
                          timeout=600, check=False)
    return proc.returncode


def cmd_archive(args) -> int:
    """Report the archive bucket, or expire what is past retention."""
    from pipeline import archive
    logging_setup.configure(None, args.verbose)
    target = archive.Target()
    if not target.configured:
        print("\n  archive storage is not configured: "
              + ", ".join(target.missing()) + "\n")
        return 0
    if args.restore:
        landed = archive.restore_episode(args.restore, config.EPISODES_DIR)
        print(f"\n  restored {len(landed)} files into "
              f"episodes/{args.restore}/")
        for name in landed:
            print(f"    {name}")
        print()
        return 0

    if args.restore_library:
        count = archive.restore_library()
        print(f"\n  restored {count} pictures into the library\n")
        return 0

    if args.backup:
        from pipeline.manifest import Episode
        stored = 0
        for directory in sorted(p for p in config.EPISODES_DIR.iterdir()
                                if p.is_dir()):
            if not (directory / "manifest.json").exists():
                continue
            ep = Episode(directory.name, config.EPISODES_DIR)
            try:
                m = ep.load_manifest()
            except Exception as exc:
                print(f"    {directory.name}: {exc}")
                continue
            stored += len(archive.archive_record(ep, m, target))
        shelf = archive.archive_library(target)
        print(f"\n  {stored} record files stored")
        print(f"  library: {len(shelf.get('stored', []))} stored, "
              f"{shelf.get('skipped', 0)} already there\n")
        return 0

    if args.prune:
        result = archive.expire(target, dry_run=args.dry_run)
        if not result["enabled"]:
            print(f"\n  {result['note']}\n")
            return 0
        verb = "would free" if args.dry_run else "freed"
        print(f"\n  {verb} {result['freed_mb']} MB from "
              f"{len(result['removed'])} objects past "
              f"{result['retain_days']:g} days; {result['kept']} kept\n")
        for row in result["removed"][:20]:
            print(f"    {row['key']:48} {row['mb']:7} MB  "
                  f"{row['age_days']:.0f}d")
        held = result.get("held") or []
        if held:
            print(f"\n  {len(held)} kept past their age because the "
                  "episode has not been posted:")
            for row in held[:20]:
                print(f"    {row['key']:48} {row['mb']:7} MB  "
                      f"{row['age_days']:.0f}d")
        print()
        return 0
    plan = archive.budget(0, target)
    if not plan["capped"]:
        print(f"\n  {archive.bucket_usage(target) / 1e9:.2f} GB "
              "stored, no cap set\n")
        return 0
    print(f"\n  {plan['used'] / 1e9:.2f} GB of "
          f"{plan['limit'] / 1e9:.2f} GB ({plan['percent']:.0f}%), "
          f"{plan['free'] / 1e9:.2f} GB free")
    print(f"  objects expire after {archive.retain_days():g} days\n")
    return 0


def cmd_check(args) -> int:
    """Inspect a rendered episode the way a viewer would."""
    from pipeline import checks
    ep = _episode(args.id)
    if not ep.manifest_path.exists():
        print(f"no episode {args.id}", file=sys.stderr)
        return 1
    m = ep.load_manifest()
    logging_setup.configure(ep.log, args.verbose)
    profile = profiles.load(m.profile)
    vertical = not profile.visuals.supports("16:9")
    result = checks.inspect(ep, m, vertical=vertical)
    print()
    if result["clean"]:
        print("    nothing wrong\n")
        return 0
    for finding in result["findings"]:
        mark = "BLOCKER" if finding["severity"] == "blocker" else "warning"
        print(f"    {mark:8} {finding['where']:10} {finding['message']}")
        print(f"             fix: {finding['fix']}")
    print(f"\n    {result['blockers']} blocking, "
          f"{result['warnings']} worth a look\n")
    return 1 if result["blockers"] else 0


def cmd_prune(args) -> int:
    """Remove working files, and videos that are safely elsewhere."""
    from pipeline import prune
    logging_setup.configure(None, args.verbose)
    result = prune.sweep(config.EPISODES_DIR, dry_run=args.dry_run)
    if not result["enabled"]:
        print(f"\n  {result['note']}\n")
        return 0
    verb = "would free" if args.dry_run else "freed"
    print(f"\n  {verb} {result['freed_mb']} MB from "
          f"{len(result['episodes'])} episodes\n")
    for row in result["episodes"]:
        what = ", ".join(row["working"]) or "-"
        print(f"    {row['episode']:14} {row['mb']:7} MB  {what}"
              f"{'  + video' if row['video'] else ''}")
    for row in result["kept"]:
        print(f"    {row['episode']:14} kept: {row['why']}")
    print()
    return 0


def cmd_batch(args) -> int:
    """Draft several episodes and give each a release time.

    The point of a queue: the work happens when you have time, the
    posting happens on schedule. Each episode is drafted to Gate 1 and
    stops there -- a batch creates work to review, never work that
    publishes itself.
    """
    from pipeline import schedule
    logging_setup.configure(None, args.verbose)
    settings = config.load()
    profile = args.profile or settings.profile
    profiles.load(profile)

    count = args.count or config.BATCH_SIZE
    try:
        releases = schedule.plan_releases(count)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    mode = args.publish_mode
    print(f"\n  Drafting {count} episodes for {profile}, "
          f"releasing {len(config.PUBLISH_SLOTS)} a day\n")

    made = 0
    for when in releases:
        episode_id = f"{when:%Y%m%d}-{when:%H%M}"
        ep = _episode(episode_id)
        if ep.manifest_path.exists():
            print(f"    {episode_id}  exists, skipped")
            continue
        try:
            ep.ensure_dirs()
            m = Manifest.new(episode_id, recipe=args.recipe,
                             profile=profile, voice=settings.voice)
            m.publish_mode = mode
            m.publish_after = when.isoformat()
            log = logging_setup.get(episode_id, "batch")
            export.run(ep, m, {})
            ep.save_manifest(m)
            script_gen.run(ep, m, engine=args.script_engine,
                           profile=profiles.load(profile))
            from pipeline.scrub import assert_clean
            assert_clean([ep.snapshot, ep.script])
            m.mark_done("scrub", findings=0)
            ep.save_manifest(m)
            made += 1
            print(f"    {episode_id}  drafted, releases {when:%a %d %b %H:%M}")
            log.info("drafted in a batch")
        except PipelineError as exc:
            print(f"    {episode_id}  FAILED: {exc}")
            if not args.keep_going:
                raise
        if made >= count:
            break

    print(f"\n  {made} drafted. Each stops at Gate 1 until you approve it.")
    print("  Review them:  python run.py dashboard\n")
    return 0


def cmd_publish_due(args) -> int:
    """Publish every approved episode whose slot has arrived.

    Safe to run on a timer. It publishes only what a person has already
    approved -- gates.py refuses to clear a gate during a scheduled run,
    so this can never approve anything itself.
    """
    from pipeline import schedule
    logging_setup.configure(None, args.verbose)
    log = logging_setup.get(None, "publish-due")

    manifests = []
    for episode_id in sorted(_episode_ids()):
        ep = _episode(episode_id)
        try:
            manifests.append((ep, ep.load_manifest()))
        except PipelineError as exc:
            log.warning(f"{episode_id}: {exc}")

    due = schedule.due_episodes([m for _, m in manifests])
    if not due:
        print("\n  nothing due\n")
        return 0

    published = 0
    for ep, m in manifests:
        if m not in due:
            continue
        print(f"\n  {m.id}: {m.title}")
        if args.dry_run:
            print(f"    would publish ({m.publish_mode})")
            continue
        try:
            upload.run(ep, m, dry_run=False)
            if not args.no_instagram:
                instagram.run(ep, m, dry_run=False)
            ep.save_manifest(m)
            notify.recovered(m.id, "upload")
            published += 1
            print("    published")
        except PipelineError as exc:
            ep.save_manifest(m)
            exc.episode_id, exc.stage = m.id, "upload"
            notify.failure(m.id, "upload", exc, str(ep.log))
            print(f"    FAILED: {exc}")
    print(f"\n  {published} of {len(due)} published\n")
    return 0 if published == len(due) else 1


def cmd_notify_test(args) -> int:
    ok, detail = notify.test_channel()
    print(f"\n  alerts: {'ok' if ok else 'not working'}  —  {detail}\n")
    if not ok:
        print("  Set SMTP_HOST, SMTP_USER, SMTP_APP_PASSWORD and ALERT_EMAIL")
        print("  in .env. For Gmail, SMTP_APP_PASSWORD must be an app")
        print("  password, not the account password.\n")
    return 0 if ok else 1


def _fail(exc: BaseException) -> int:
    """Report a failure to the user, by mail if they configured it.

    Alerting lives here because this is the only point both dispatch paths
    pass through, and because a failure that never reached the user is the
    one thing an unattended pipeline cannot afford.
    """
    notify.failure(getattr(exc, "episode_id", None),
                   getattr(exc, "stage", None), exc,
                   getattr(exc, "log_path", None))
    print(f"\n  {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"  {recovery.describe(exc)}", file=sys.stderr)
    fix = notify.remedy(exc)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)
    print(file=sys.stderr)
    return getattr(exc, "exit_code", 1)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `run.py 001 ...` is the common path; route it without a subcommand.
    if argv and not argv[0].startswith("-") and argv[0] not in {
        "new", "list", "status", "doctor", "voices", "notify-test",
        "dashboard", "trends", "health", "publish-due", "batch", "connect", "prune", "check", "archive"
    }:
        rp = argparse.ArgumentParser(prog="run.py <id>")
        rp.add_argument("id")
        rp.add_argument("--from", dest="start", choices=STAGES)
        rp.add_argument("--stage", choices=STAGES)
        rp.add_argument("--force", action="store_true")
        # Separate from --force on purpose. --force means re-run the
        # stage; re-running the images stage keeps the pictures it
        # already has, because discarding eleven good ones to re-run a
        # later stage is never what was meant -- and on a free
        # generator those are eleven requests you may not get back.
        rp.add_argument("--refetch-images", action="store_true",
                        help="draw every scene picture again")
        rp.add_argument("--redraw", metavar="N[,N...]",
                        help="draw these scenes' pictures again, and "
                             "nothing else. e.g. --redraw 3,14")
        # Only a person can answer this, because the narrow upload scope
        # cannot read the channel to find out.
        rp.add_argument("--privacy",
                        choices=("private", "unlisted", "public"),
                        help="how this upload goes out; the default is "
                             "YOUTUBE_PRIVACY, itself private")
        rp.add_argument("--assume-not-posted", action="store_true",
                        help="you checked the channel and the video is "
                             "not there; skip the reconcile")
        rp.add_argument("--approve-script", action="store_true")
        rp.add_argument("--approve", action="store_true")
        rp.add_argument("--publish", action="store_true")
        rp.add_argument("--no-retry", action="store_true",
                        help="fail on the first network error")
        rp.add_argument("--instagram", action="store_true",
                        help="with --publish, post the reel only")
        rp.add_argument("--no-instagram", action="store_true",
                        help="with --publish, skip Instagram")
        rp.add_argument("--reel", type=int, default=1,
                        help="which vertical cut to post (default 1)")
        rp.add_argument("--status", action="store_true")
        rp.add_argument("--dry-run", action="store_true")
        rp.add_argument("--through-upload", action="store_true")
        rp.add_argument("--no-captions", action="store_true")
        rp.add_argument("--no-animate", action="store_true")
        rp.add_argument("--no-dissolve", action="store_true")
        rp.add_argument("--no-music", action="store_true")
        rp.add_argument("--no-ambience", action="store_true",
                        help="skip the per-scene ambient beds")
        rp.add_argument("--look", default="clean",
                        choices=("clean", "filmic", "none"))
        rp.add_argument("--voice-engine", default=config.load().voice_engine,
                        choices=("silent", "kokoro", "cloned"))
        rp.add_argument("--voice", default=None)
        rp.add_argument("--script-engine",
                        default=config.load().script_engine,
                        choices=script_gen.ENGINES)
        rp.add_argument("--captions", default="derive", choices=("derive", "whisper"))
        rp.add_argument("--params")
        rp.add_argument("-v", "--verbose", action="store_true")
        args = rp.parse_args(argv)
        try:
            return cmd_status(args) if args.status else cmd_run(args)
        except PipelineError as exc:
            return _fail(exc)
        except Exception as exc:  # unexpected: alert, then let it surface
            notify.failure(getattr(exc, "episode_id", None),
                           getattr(exc, "stage", None), exc)
            raise

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except PipelineError as exc:
        return _fail(exc)
    except Exception as exc:  # unexpected: alert, then let it surface
        notify.failure(None, None, exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
