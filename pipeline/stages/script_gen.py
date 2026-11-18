"""Stage 2: turn the snapshot into a scene-tagged script.

The model structures and narrates facts it is given. It never decides what
is true and never invents a number -- architecture doc, section 1.

Three engines: `template` (deterministic, offline, no key), `gemini`
(free tier), `ollama` (local). Template is the default because a fee
breakdown is formulaic and a deterministic script is testable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..config import env_int
from ..errors import ConfigError, PipelineError, StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest, Scene
from ..script_format import parse, render, to_scenes, validate_against_snapshot
from .export import load_snapshot

ENGINES = ("file", "story", "template", "gemini", "groq", "ollama")

# One deterministic template per recipe. Registered at the bottom of this
# module, once every builder is defined.
TEMPLATES: dict[str, object] = {}


def run(ep: Episode, m: Manifest, engine: str = "template", profile=None) -> None:
    log = get(m.id, "script")
    data = load_snapshot(ep)
    # Which model actually wrote it. Not recorded before, and its
    # absence cost a wrong conclusion: a story the fallback was
    # credited with had in fact been written by the primary, after the
    # fallback 404'd on a model Groq had retired.
    written: dict[str, str] = {}

    if engine == "file":
        text = _from_file(ep)
    elif engine == "story":
        text = _from_story(m, profile, record=written)
    elif engine == "template":
        builder = TEMPLATES.get(data.get("recipe", ""))
        if builder is None:
            raise ConfigError(
                f"no script template for recipe {data.get('recipe')!r}. "
                f"Known: {sorted(TEMPLATES)}"
            )
        text = builder(m, data, profile)
    elif engine == "gemini":
        text = _from_gemini(m, data, profile)
    elif engine == "groq":
        text = _ask_groq(_prompt(m, data, profile))
    elif engine == "ollama":
        text = _from_ollama(m, data, profile)
    else:
        raise ConfigError(f"unknown script engine {engine!r}; expected {ENGINES}")

    parsed = parse(text)
    scenes = to_scenes(parsed, default_voice=m.voice)
    validate_against_snapshot(scenes, data, profile)
    _check_hook(scenes, profile)
    add_outro(scenes, m.voice)

    m.scenes = scenes
    ep.script.write_text(render(scenes, m.title), encoding="utf-8")
    m.mark_done("script", engine=engine, scenes=len(scenes),
                words=sum(len(s.text.split()) for s in scenes),
                **written)
    log.info(f"{len(scenes)} scenes, {sum(len(s.text.split()) for s in scenes)} words")


# The last thing a viewer hears. Asked for on every video, so it is
# added here rather than written into each script: a line a generator
# has to remember is a line that will be missing from one in ten.
DEFAULT_OUTRO = "If you enjoyed this story, subscribe for more."


def outro_line() -> str:
    """The closing line, or empty to leave videos ending on the story."""
    raw = os.getenv("OUTRO_LINE")
    return DEFAULT_OUTRO if raw is None else raw.strip()


def add_outro(scenes: list, default_voice: str | None = None) -> bool:
    """Append the closing line as a scene of its own. True if added.

    It carries the previous scene's picture prompt rather than one of
    its own, so the closing shot is the one the story ended on and no
    extra drawing is asked of a generator that is often out of quota.

    Idempotent: re-running the script stage must not stack three
    subscribe lines on the end of an episode.
    """
    line = outro_line()
    if not line or not scenes:
        return False
    if scenes[-1].text.strip() == line:
        return False
    last = scenes[-1]
    scenes.append(Scene(
        index=last.index + 1,
        text=line,
        visual_spec=dict(getattr(last, "visual_spec", {}) or {}),
        image_prompt=getattr(last, "image_prompt", "") or "",
        voice=getattr(last, "voice", None) or default_voice,
    ))
    return True


def _check_hook(scenes, profile=None) -> None:
    """Scene 1 must state the finding, not preview it.

    Retention across the first 30 seconds is the strongest input to whether
    a long-form video is recommended -- architecture doc, section 26.

    The no-preview rule holds for everything: "in this video we will"
    loses viewers whatever the subject. The must-carry-a-figure rule does
    not. It is right for a profile whose whole claim is a number, and
    wrong for a story, where the hook is an image or a situation -- "On
    the bank of a great river stood a rose-apple tree" has no figure in
    it and is a perfectly good opening. Asked of the profile, the same
    way the tags and the disclaimer now are.
    """
    if not scenes:
        raise StageError("script produced no scenes")
    first = scenes[0].text.lower()
    banned = ("in this video", "today we", "we're going to", "we are going to",
              "let's look at", "before we begin", "hi guys", "welcome back")
    for phrase in banned:
        if phrase in first:
            raise StageError(
                f"scene 1 opens with {phrase!r}. The hook must state the finding, "
                f"not preview it -- architecture doc, section 26."
            )
    binds = True
    if profile is not None:
        binds = getattr(profile.visuals, "binds_data_keys", lambda: True)()
    if binds and not any(ch.isdigit() for ch in scenes[0].text):
        raise StageError(
            "scene 1 contains no number. The opening claim must carry a figure."
        )


# ---- template engine ---------------------------------------------------


def _from_template(m: Manifest, data: dict, profile) -> str:
    """Deterministic script for the fee_drag recipe.

    Numbers are formatted from the snapshot, never written by hand, so the
    script cannot disagree with the charts.
    """
    money = profile.style.money if profile else (lambda v, d=0: f"{v:,.{d}f}")
    trade = data["trade"]
    charges = data["charges"]
    ladder = data["ladder"]
    small = ladder[0]
    big = ladder[-1]

    biggest = max(
        ((k, v) for k, v in charges.items() if k != "total"),
        key=lambda kv: kv[1],
    )
    label = {"brokerage": "brokerage", "stt": "STT", "gst": "GST",
             "exchange_txn": "the exchange charge", "sebi_fee": "the SEBI fee",
             "stamp_duty": "stamp duty"}.get(biggest[0], biggest[0])

    return f"""# {data['title']}

[SCENE 1 | chart: breakeven]
Your option has to move {data['breakeven_points']} points before you have made
a single rupee. Not before you have made a profit worth having. Before you
have made anything at all.

[SCENE 2 | chart: fee_stack | annotate: stt, gst]
Here is every charge on one round trip. One lot, {trade['quantity']} quantity,
bought at {money(trade['entry_premium'])} and sold at {money(trade['exit_premium'])}.
Six separate line items, {money(charges['total'], 2)} in total.

[SCENE 3 | chart: fee_stack]
The largest single item is {label}, at {money(biggest[1], 2)}. Most people
assume the taxes are the problem. On a trade this size, the flat fee is.

[SCENE 4 | chart: gross_vs_net]
Gross profit on that move was {money(data['gross_pnl'])}. After charges you keep
{money(data['net_pnl'])}. That is {data['drag_pct']} percent of the gain gone
before it reached you.

[SCENE 5 | chart: drag_ladder]
Now watch what happens as the move gets smaller. On a {small['move_points']} point
move, charges take {small['drag_pct']} percent of the gross. On a
{big['move_points']} point move, {big['drag_pct']} percent. The cost is almost
fixed. Your profit is not.

[SCENE 6 | chart: breakeven]
Which is the real point. Costs do not scale down with your winners. A small
win is mostly fees, and a strategy that wins small and often can lose money
while showing a positive win rate.

[SCENE 7 | chart: gross_vs_net]
Every rate used here comes from the published schedules, and your broker's
own tariff will differ. Run the arithmetic on your own numbers before you
decide what a trade is worth. Net of fees is the only figure that counts.
"""


# ---- LLM engines -------------------------------------------------------



# What a model has to be told to write a usable picture prompt.
#
# Every rule here exists because its absence produced something wrong on
# screen: smiling faces over a line about illness, a character who
# changed age between scenes, five consecutive close-ups, a subject on
# bare ground with nothing behind it.
PICTURE_CONTRACT = """
PICTURES

Give every scene exactly one IMAGE line, under the scene header and
above the narration:

    IMAGE: <what the viewer sees>

- Describe what is visible, not what it means. "a boy counting coins on
  a doorstep" can be drawn; "the weight of his debt" cannot.
- Name the feeling in the face and the posture, and match it to the line
  being read. A smiling face over a line about illness is the one
  mistake every viewer notices.
- Re-describe each recurring person in full every time -- age, build,
  hair, clothing. Nothing remembers scene 2 while drawing scene 7, so a
  character stays the same person only if every prompt says who they are.
- Name the shot: close-up, medium shot, wide shot. Vary it. Consecutive
  close-ups are what makes a sequence feel like a slideshow.
- Name the place and the light: inside or outside, time of day, weather.
  A subject against empty ground reads as dead on screen.
- No words, letters, signs or logos in the picture.
- No real named people and no brands.
- One or two clauses, under forty words.
"""

def wants_picture_prompts(profile) -> bool:
    """Whether this profile's frames are pictures rather than charts.

    The same attribute assemble and the images stage dispatch on, so a
    profile cannot be asked for prompts it has no use for.
    """
    return getattr(profile, "MOTION", None) == "kenburns"


STORIES_DIR = Path(__file__).resolve().parent.parent.parent / "stories"


def story_path(episode_id: str) -> Path:
    """Where a committed script for this episode would live.

    `episodes/` is gitignored, correctly -- it fills with video. So a
    script written there can never be seen by a machine that is not
    this one, and the whole pipeline stays tied to one laptop. A script
    is a few kilobytes of text: it belongs in the repository, and from
    there any runner can read it.
    """
    return STORIES_DIR / f"{episode_id}.md"


# What a model has to be told to write one of these well.
#
# Taken from the tellings the user chose as the standard, and from four
# rounds of getting it wrong: first too literary, then too terse, then
# describing what the picture already shows. The register is a
# children's fable told plainly, because the pictures carry the scene
# and the words carry the story.
STORY_CONTRACT = """
You are writing a short narrated story for a video. The viewer sees an
illustration for every line, so the words carry the STORY and never
describe the scenery.

TELLING

- Open with "Once upon a time" or an equally plain opening. No preamble,
  no "in this video", no addressing the viewer.
- Short, plain sentences. This is heard once, not read.
- Use dialogue. "O King!" said the rabbit. It carries character faster
  than description.
- Do not describe what the picture shows. The image already shows the
  green forest; the narration says what happened in it.
- No literary flourish, no interiority, no long clauses. A child should
  follow it easily and an adult should not wince.
- End with the moral, stated in exactly this form, as its own scene:
  "The moral of the story. <the lesson>."

SHAPE

- Between 18 and 32 scenes.
- One or two short sentences per scene. Roughly 8 to 14 words.
- Every scene gets its own IMAGE line.

FORMAT, exactly

# <Story title>

[SCENE 1]

IMAGE: <what this scene shows>

<the narration for this scene>

[SCENE 2]

IMAGE: ...

PICTURES

- Say what is visible: who, where, doing what, and the feeling on their
  face. Name the shot: close-up, medium shot, wide shot. Vary it.
- Say HOW MANY. "one single brown monkey, alone" -- not "the monkey".
  A prompt that does not say one has produced two.
- Re-describe each recurring character in full every time: nothing
  remembers scene 2 while drawing scene 9, so the monkey stays the same
  monkey only if every prompt says what he looks like.
- Never put a camera inside an object. "cutaway view inside the pot"
  produced an animal inside the pot.
- End every IMAGE line with: storybook illustration
- No words, letters or signs in the picture.

Output only the script. No explanation, no code fences.
"""


def story_prompt(m: Manifest, profile=None) -> str:
    """Ask for one story, by name."""
    source = getattr(profile, "STORY_SOURCE", "") if profile else ""
    whose = f" from the {source}" if source else ""
    return (f"{STORY_CONTRACT}\n\n"
            f"Write the well-known story{whose} called: {m.title}\n\n"
            "Tell the traditional story faithfully. Do not invent a new "
            "plot, and do not copy any modern translation word for word "
            "-- tell it in your own plain words.\n")


def _from_story(m: Manifest, profile, record: dict | None = None) -> str:
    """A fable, written to order by whichever model is configured.

    The template engine knows two chart recipes and nothing else, which
    is why a scheduled draft produced a script about trading costs on a
    channel that tells fables. This asks for the thing the channel
    actually makes.
    """
    if not (m.title or "").strip():
        raise ConfigError(
            "the story engine writes a story by name, and this episode "
            "has no title. Create it with --title \"The Thirsty Crow\"."
        )
    prompt = story_prompt(m, profile)
    log = get(m.id, "script")
    # Gemini first, because its prose is the register this channel was
    # tuned to over four rounds of rewriting. Groq second, because
    # Gemini's free tier returned 504 on one scheduled run in four and
    # a story that cannot be written is a slot that publishes nothing.
    # Whichever is asked second only ever runs when the first has
    # already failed, so a healthy day costs exactly one request.
    # Order is settable so the fallback can be exercised deliberately.
    # A second model that is only ever reached when the first fails is
    # a second model nobody has tested, and the moment you find out it
    # was misconfigured is the moment you needed it.
    writers = {"gemini": (_ask_gemini, "GEMINI_API_KEY"),
               "groq": (_ask_groq, "GROQ_API_KEY"),
               "ollama": (_ask_ollama, "OLLAMA_HOST")}
    order = [n.strip().lower()
             for n in os.getenv("SCRIPT_ORDER", "gemini,groq,ollama").split(",")
             if n.strip()]
    unknown = [n for n in order if n not in writers]
    if unknown:
        raise ConfigError(
            f"SCRIPT_ORDER names {', '.join(unknown)}; "
            f"expected any of {', '.join(writers)}")
    attempts = [(name, writers[name][0]) for name in order
                if os.getenv(writers[name][1])]
    if not attempts:
        raise ConfigError(
            "the story engine needs a model: set GEMINI_API_KEY or "
            "GROQ_API_KEY (both have a free tier), or run ollama and "
            "set OLLAMA_HOST."
        )
    failures: list[tuple[str, Exception]] = []
    for name, ask in attempts:
        try:
            text = ask(prompt)
        except Exception as exc:
            # Wrapped at the boundary, because beyond it the pipeline's
            # own machinery stops working. `with_retry` catches only
            # PipelineError, so google's DeadlineExceeded escaped it
            # entirely: every 504 from Gemini was reported as failing
            # "in pipeline" and retried not once, though `classify`
            # calls it transient and a second attempt often works.
            failures.append((name, _as_stage_error(name, exc)))
            log.warning(f"{name} could not write it: "
                        f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        if record is not None:
            record["written_by"] = name
        if name != attempts[0][0]:
            log.info(f"written by {name} after {attempts[0][0]} failed")
        return text
    raise _worth_raising(failures)


def _as_stage_error(writer: str, exc: Exception) -> Exception:
    """A third party's exception, carried in this pipeline's own kind.

    The message is kept whole: `classify` reads it to decide whether a
    retry is worth trying, and "504" in the text is the whole signal.
    """
    if isinstance(exc, PipelineError):
        return exc
    return StageError(f"{writer}: {type(exc).__name__}: {exc}")


def _worth_raising(failures: list[tuple[str, Exception]]) -> Exception:
    """Of several failures, the one the caller can best act on.

    Raising whichever came first threw away the useful one. Groq was
    refused at Cloudflare's door -- a 403, not retryable -- and Gemini
    returned a 504, which is. Because the 403 was raised, the retry
    around this stage said "unrecognised, so not retried" and gave up
    on an attempt that a second try would very likely have completed.

    A run is worth retrying if ANY writer failed in a way that passes.
    """
    from ..recovery import classify

    if not failures:                       # cannot happen; not a crash
        return StageError("the script was not written and nothing said why")
    rank = {"transient": 0, "later": 1, "needs_human": 2}
    name, exc = min(failures,
                    key=lambda f: rank.get(classify(f[1]).kind, 3))
    if len(failures) > 1:
        others = "; ".join(
            # The writer's name is already on a wrapped failure; adding
            # it again reads as "groq: groq: ...".
            (str(e)[:220] if str(e).startswith(f"{n}:") else f"{n}: {str(e)[:200]}")
            for n, e in failures if n != name)
        exc = type(exc)(f"{exc} (also tried -- {others})") \
            if isinstance(exc, PipelineError) else exc
    return exc


def _from_file(ep: Episode) -> str:
    """The script you wrote, taken as it is.

    The other engines make a script out of data. For a story that
    already exists -- a fable, a piece of history, anything with a text
    behind it -- there is nothing to generate: the writing is the work,
    and it is yours. This engine exists so bringing your own script is a
    first-class way to use the pipeline rather than something you have
    to fake by editing a manifest by hand.

    Everything after this is unchanged. The script is parsed by the same
    rules, the same checks run, and Gate 1 still asks a person.
    """
    path = ep.script
    if not path.exists():
        # Then the committed one, which is what a cloud runner has.
        shared = story_path(ep.id)
        if shared.exists():
            path = shared
    if not path.exists():
        raise ConfigError(
            f"no script at {ep.script} or {story_path(ep.id)}. "
            "The 'file' engine reads a script you wrote; write it in "
            "either place, with a [SCENE n] header per scene and an "
            "IMAGE: line saying what each one shows. stories/ is "
            "committed, so a cloud render can see it."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ConfigError(f"{path} is empty")
    return text


def _prompt(m: Manifest, data: dict, profile) -> str:
    template = ""
    if profile is not None:
        tpl = Path(profile.__path__[0]) / "script.md"
        if tpl.exists():
            template = tpl.read_text(encoding="utf-8")
    pictures = PICTURE_CONTRACT if wants_picture_prompts(profile) else ""
    return f"""{template}
{pictures}
TITLE: {m.title}

DATA (the only facts you may use; never invent a number):
{json.dumps(data, indent=2)[:6000]}

Write the script now. Output only the scene-tagged script, nothing else.
"""


def _ask_gemini(prompt: str) -> str:
    """Send a prompt to Gemini and return the text.

    Split out so the story engine and the data-shaped engines share one
    transport rather than one growing two copies of it.
    """
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ConfigError("GEMINI_API_KEY is not set")
    # `google-genai`, not `google-generativeai`. The older package was
    # declared end of life -- "no longer receiving updates or bug
    # fixes" -- while being the primary writer of a channel that
    # publishes unattended twice a day. A library nobody is fixing
    # breaks on a Sunday.
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ConfigError(
            "google-genai is not installed. pip install google-genai"
        ) from exc

    # The client's default deadline is measured in seconds and a
    # twenty-five scene story does not always come back inside it.
    # HttpOptions takes milliseconds.
    timeout = env_int("GEMINI_TIMEOUT_SEC", 300)
    client = genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=timeout * 1000))
    wanted = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

    def ask(model_name):
        return client.models.generate_content(
            model=model_name, contents=prompt)

    try:
        resp = ask(wanted)
    except Exception as exc:
        # A 504 from the service itself, not the deadline: the request
        # was given 300 seconds and the API still gave up. A retry
        # against the same model is a retry against the same overload,
        # so a second model is the only thing that changes the odds.
        spare = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
        if not spare or spare == wanted:
            raise
        get(None, "script").warning(
            f"{type(exc).__name__} from {wanted}; asking {spare}")
        resp = ask(spare)
    text = getattr(resp, "text", None)
    if not text:
        raise StageError("Gemini returned an empty response")
    # A model asked for a script sometimes wraps it in a fence anyway.
    return text.strip().removeprefix("```markdown").removeprefix(
        "```").removesuffix("```").strip()


USER_AGENT = "net-of-fees/1.0 (+https://github.com/Chandan-c10)"

# Groq's named replacement for the decommissioned llama-3.3-70b.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"


def _groq_models(key: str) -> str:
    """Which models this key can actually use, for a 404 message.

    Asked only when a model has just been refused, so the reader is one
    setting away from working rather than one search away.
    """
    import json as _json
    import urllib.request
    try:
        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}",
                     "User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=30) as resp:
            rows = _json.loads(resp.read()).get("data", [])
    except Exception:
        return "(could not list them)"
    ids = sorted(r.get("id", "") for r in rows if r.get("id"))
    return ", ".join(ids[:12]) or "(none)"


def _ask_groq(prompt: str) -> str:
    """The same, against Groq.

    Plain HTTP against the OpenAI-compatible endpoint rather than another
    client library: one more dependency to install on every runner, for
    one POST, is a poor trade. The free tier allows a thousand requests
    a day against Gemini's twenty, which is the whole reason it is here.
    """
    import json as _json
    import urllib.error
    import urllib.request
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise ConfigError("GROQ_API_KEY is not set")
    payload = _json.dumps({
        # Groq retires models and the old default, llama-3.3-70b-versatile,
        # was decommissioned in August. A 404 from a hardcoded model name
        # is a silent fallback failure: the channel kept publishing
        # because Gemini caught it, so nobody learned the spare tyre was
        # flat until Gemini was down too.
        "model": os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(os.getenv("GROQ_TEMPERATURE", "0.8")),
    }).encode()
    # The User-Agent is not decoration. Groq sits behind Cloudflare,
    # which refuses urllib's default signature with "403 error code:
    # 1010" -- a bot block, returned before the request reaches Groq at
    # all. The first run against Groq failed that way and read as a bad
    # key, which it was not.
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT,
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(
                request, timeout=env_int("GROQ_TIMEOUT_SEC", 180)) as resp:
            body = _json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode(errors="replace")
        if exc.code == 401:
            raise StageError(
                "groq refused the key: GROQ_API_KEY is wrong or revoked"
            ) from exc
        if exc.code == 403 and "1010" in detail:
            raise StageError(
                "groq's front door refused this client (Cloudflare 1010); "
                "the request needs a User-Agent header"
            ) from exc
        if exc.code == 404:
            raise StageError(
                f"groq has no model {os.getenv('GROQ_MODEL', DEFAULT_GROQ_MODEL)!r}"
                f" -- it was probably retired. Set GROQ_MODEL to one of: "
                f"{_groq_models(key)}") from exc
        raise StageError(f"groq returned {exc.code}: {detail}") from exc
    except Exception as exc:
        raise StageError(f"could not reach groq: {exc}") from exc
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise StageError(f"groq returned no script: {str(body)[:200]}") from exc
    if not text.strip():
        raise StageError("groq returned an empty response")
    return text.strip().removeprefix("```markdown").removeprefix(
        "```").removesuffix("```").strip()


def _ask_ollama(prompt: str) -> str:
    """The same, against a local model."""
    import json as _json
    import os
    import urllib.request
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    payload = _json.dumps({
        "model": os.getenv("OLLAMA_MODEL", "llama3.1"),
        "prompt": prompt, "stream": False,
    }).encode()
    request = urllib.request.Request(
        f"{host}/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=600) as resp:
            text = _json.loads(resp.read()).get("response", "")
    except Exception as exc:
        raise StageError(f"could not reach ollama at {host}: {exc}") from exc
    if not text.strip():
        raise StageError("ollama returned an empty response")
    return text.strip().removeprefix("```markdown").removeprefix(
        "```").removesuffix("```").strip()


def _from_gemini(m: Manifest, data: dict, profile) -> str:
    import os
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Use --script-engine template for an "
            "offline run, or export the key."
        )
    # One transport, shared with the story engine: the deadline, the
    # fallback model and the fence-stripping are worth having here too,
    # and two copies of a client is two places to migrate next time.
    return _ask_gemini(_prompt(m, data, profile))


def _from_ollama(m: Manifest, data: dict, profile) -> str:
    import os
    import urllib.request
    host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3.1")
    body = json.dumps({
        "model": model,
        "prompt": _prompt(m, data, profile),
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{host}/api/generate", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = json.loads(r.read())
    except OSError as exc:
        raise ConfigError(
            f"cannot reach Ollama at {host}. Start it with `ollama serve`, "
            f"or use --script-engine template"
        ) from exc
    text = payload.get("response", "")
    if not text.strip():
        raise StageError(f"Ollama model {model!r} returned nothing")
    return text


def _from_template_incidents(m: Manifest, data: dict, profile) -> str:
    """Deterministic script for the incident_log recipe.

    Every figure is formatted from the snapshot, so the narration cannot
    disagree with the charts it sits over.
    """
    worst = data["worst"]
    layers = list(data["by_layer"])
    top_layer = layers[0]
    found = data["by_detection"]
    biggest_source = max(found, key=found.get)

    return f"""# {data.get('title') or m.title}

[SCENE 1 | chart: detection_gap]
Eight bugs reached my live trading system. My test suite caught one of
them. The other seven were found by the system doing something I could
see was wrong, and the median one had been running for
{data['median_days_hidden']} days before I noticed.

[SCENE 2 | chart: detection_gap]
That is the number worth sitting with. Not eight bugs. Twenty seven days.
A bug in a web app announces itself. A bug in a trading system just
quietly makes slightly worse decisions, and slightly worse looks exactly
like a bad week.

[SCENE 3 | chart: severity_split]
{data['serious']} of the {data['total']} produced wrong output or put money
at risk. The worst one was {worst['title'].lower()}. It sat in the
{worst['layer']} layer for {worst['days_hidden']} days, and every result I
looked at during that time was better than reality.

[SCENE 4 | chart: by_layer]
Here is where they actually lived. {top_layer.title()} took the most.
Not the strategy. Not the signal logic. The plumbing between deciding to
trade and the trade existing.

[SCENE 5 | chart: by_detection]
And here is how they were found. Tests caught {data['caught_by_tests_pct']}
percent. The rest came from {biggest_source.replace('_', ' ')} and from
recomputing numbers by hand that the system had already reported.

[SCENE 6 | chart: architecture]
The pattern is consistent. The bugs were not in the clever part. They were
in the boring part, where an integer divided to zero, a clock disagreed
with another clock, and a retry sent an order the broker had already
accepted.

[SCENE 7 | chart: by_detection]
What changed is that I stopped trusting the system to tell me it was
healthy. Reconciliation against the broker statement runs daily now, and
it has found more than my tests ever did. I expect the next eight bugs to
hide just as well as these did.
"""


TEMPLATES.update({
    "fee_drag": _from_template,
    "incident_log": _from_template_incidents,
})
