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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import config, logging_setup
from pipeline.errors import GateError, PipelineError
from pipeline.manifest import STAGES, Episode, Manifest
from pipeline.recipes import available as available_recipes
from pipeline.stages import (assemble, captions, charts, export, finish,
                             metadata, script_gen, shorts, thumbnail,
                             upload, voice)

import profiles


def _episode(episode_id: str) -> Episode:
    return Episode(episode_id, config.EPISODES_DIR)


def _next_id() -> str:
    config.EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    existing = [d.name for d in config.EPISODES_DIR.iterdir() if d.is_dir()]
    numbers = [int(n) for n in existing if n.isdigit()]
    return f"{max(numbers, default=0) + 1:03d}"


# ---- stage dispatch ---------------------------------------------------


def _run_stage(name: str, ep: Episode, m: Manifest, settings, args) -> None:
    profile = profiles.load(m.profile)
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
    elif name == "charts":
        charts.run(ep, m, profile)
    elif name == "captions":
        captions.run(ep, m, method=args.captions)
    elif name == "assemble":
        assemble.run(ep, m, burn_captions=not args.no_captions,
                     profile=profile, animate=not args.no_animate,
                     dissolve=not args.no_dissolve,
                     music=not args.no_music)
        finish.run(ep, m, look_name=args.look)
    elif name == "metadata":
        metadata.run(ep, m, profile)
    elif name == "thumbnail":
        thumbnail.run(ep, m, profile)
    elif name == "shorts":
        shorts.run(ep, m, profile)
    elif name == "upload":
        upload.run(ep, m, dry_run=args.dry_run)
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

    if m.is_done(name) and not args.force:
        log.debug(f"{name}: already done, skipping")
        return True

    try:
        _run_stage(name, ep, m, settings, args)
    except PipelineError as exc:
        m.mark_failed(name, str(exc))
        ep.save_manifest(m)
        raise
    ep.save_manifest(m)
    return True


# ---- commands ---------------------------------------------------------


def cmd_new(args) -> int:
    episode_id = args.id or _next_id()
    ep = _episode(episode_id)
    if ep.manifest_path.exists() and not args.force:
        print(f"episode {episode_id} already exists; use --force to overwrite",
              file=sys.stderr)
        return 1
    ep.ensure_dirs()
    settings = config.load()
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
        todo = ["upload"]
    else:
        start = STAGES.index(args.start) if args.start else 0
        todo = list(STAGES[start:])
        if not args.through_upload:
            todo = [s for s in todo if s != "upload"]

    for name in todo:
        if not _execute(ep, m, name, settings, args, log):
            break
    _print_status(m, ep)
    return 0


def _approve_gate_1(ep: Episode, m: Manifest, log) -> int:
    """Gate 1. Enforced here, not merely displayed."""
    if not m.is_done("script"):
        raise GateError("the script stage is not complete")
    if not m.is_done("scrub"):
        raise GateError("the scrub stage has not passed")
    flagged = [s.index for s in m.scenes if s.flagged]
    if flagged:
        raise GateError(f"scenes still flagged: {flagged}")

    data = json.loads(ep.snapshot.read_text())
    from pipeline.script_format import validate_against_snapshot
    validate_against_snapshot(m.scenes, data, profiles.load(m.profile))

    m.pass_gate_1()
    ep.save_manifest(m)
    log.info("gate 1 cleared")
    print(f"Gate 1 cleared. Next:  python run.py {m.id}")
    return 0


def _approve_gate_2(ep: Episode, m: Manifest, log) -> int:
    """Gate 2. The upload stage independently refuses a null approved_at."""
    for required in ("assemble", "metadata", "thumbnail"):
        if not m.is_done(required):
            raise GateError(f"{required} is not complete")
    if not m.gate_1_passed_at:
        raise GateError("gate 1 was never cleared")
    if m.stage("scrub").data.get("findings"):
        raise GateError("scrub reported findings")
    if m.stage("voice").data.get("draft"):
        raise GateError(
            "this episode was narrated with the draft voice engine. "
            "Re-run `--stage voice --force` with a real engine before approving."
        )

    m.pass_gate_2()
    ep.save_manifest(m)
    log.info("gate 2 cleared")
    print(f"Gate 2 cleared. Next:  python run.py {m.id} --publish")
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
    from pipeline.voices.kokoro import (DEFAULT_VOICE, available_voices,
                                        is_installed)

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
    sub.add_parser("voices", help="list available voices").set_defaults(func=cmd_voices)

    st = sub.add_parser("status", help="show one episode")
    st.add_argument("id")
    st.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `run.py 001 ...` is the common path; route it without a subcommand.
    if argv and not argv[0].startswith("-") and argv[0] not in {
        "new", "list", "status", "doctor", "voices"
    }:
        rp = argparse.ArgumentParser(prog="run.py <id>")
        rp.add_argument("id")
        rp.add_argument("--from", dest="start", choices=STAGES)
        rp.add_argument("--stage", choices=STAGES)
        rp.add_argument("--force", action="store_true")
        rp.add_argument("--approve-script", action="store_true")
        rp.add_argument("--approve", action="store_true")
        rp.add_argument("--publish", action="store_true")
        rp.add_argument("--status", action="store_true")
        rp.add_argument("--dry-run", action="store_true")
        rp.add_argument("--through-upload", action="store_true")
        rp.add_argument("--no-captions", action="store_true")
        rp.add_argument("--no-animate", action="store_true")
        rp.add_argument("--no-dissolve", action="store_true")
        rp.add_argument("--no-music", action="store_true")
        rp.add_argument("--look", default="clean",
                        choices=("clean", "filmic", "none"))
        rp.add_argument("--voice-engine", default=config.load().voice_engine,
                        choices=("silent", "kokoro", "cloned"))
        rp.add_argument("--voice", default=None)
        rp.add_argument("--script-engine", default=config.load().script_engine)
        rp.add_argument("--captions", default="derive", choices=("derive", "whisper"))
        rp.add_argument("--params")
        rp.add_argument("-v", "--verbose", action="store_true")
        args = rp.parse_args(argv)
        try:
            return cmd_status(args) if args.status else cmd_run(args)
        except PipelineError as exc:
            print(f"\n  {type(exc).__name__}: {exc}\n", file=sys.stderr)
            return exc.exit_code

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except PipelineError as exc:
        print(f"\n  {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
