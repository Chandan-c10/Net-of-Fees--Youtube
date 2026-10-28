"""Stage 2: turn the snapshot into a scene-tagged script.

The model structures and narrates facts it is given. It never decides what
is true and never invents a number -- architecture doc, section 1.

Three engines: `template` (deterministic, offline, no key), `gemini`
(free tier), `ollama` (local). Template is the default because a fee
breakdown is formulaic and a deterministic script is testable.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import ConfigError, StageError
from ..logging_setup import get
from ..manifest import Episode, Manifest
from ..script_format import parse, render, to_scenes, validate_against_snapshot
from .export import load_snapshot

ENGINES = ("template", "gemini", "ollama")

# One deterministic template per recipe. Registered at the bottom of this
# module, once every builder is defined.
TEMPLATES: dict[str, object] = {}


def run(ep: Episode, m: Manifest, engine: str = "template", profile=None) -> None:
    log = get(m.id, "script")
    data = load_snapshot(ep)

    if engine == "template":
        builder = TEMPLATES.get(data.get("recipe", ""))
        if builder is None:
            raise ConfigError(
                f"no script template for recipe {data.get('recipe')!r}. "
                f"Known: {sorted(TEMPLATES)}"
            )
        text = builder(m, data, profile)
    elif engine == "gemini":
        text = _from_gemini(m, data, profile)
    elif engine == "ollama":
        text = _from_ollama(m, data, profile)
    else:
        raise ConfigError(f"unknown script engine {engine!r}; expected {ENGINES}")

    parsed = parse(text)
    scenes = to_scenes(parsed, default_voice=m.voice)
    validate_against_snapshot(scenes, data, profile)
    _check_hook(scenes)

    m.scenes = scenes
    ep.script.write_text(render(scenes, m.title), encoding="utf-8")
    m.mark_done("script", engine=engine, scenes=len(scenes),
                words=sum(len(s.text.split()) for s in scenes))
    log.info(f"{len(scenes)} scenes, {sum(len(s.text.split()) for s in scenes)} words")


def _check_hook(scenes) -> None:
    """Scene 1 must state the finding, not preview it.

    Retention across the first 30 seconds is the strongest input to whether
    a long-form video is recommended -- architecture doc, section 26.
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
    if not any(ch.isdigit() for ch in scenes[0].text):
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


def _prompt(m: Manifest, data: dict, profile) -> str:
    template = ""
    if profile is not None:
        tpl = Path(profile.__path__[0]) / "script.md"
        if tpl.exists():
            template = tpl.read_text(encoding="utf-8")
    return f"""{template}

TITLE: {m.title}

DATA (the only facts you may use; never invent a number):
{json.dumps(data, indent=2)[:6000]}

Write the script now. Output only the scene-tagged script, nothing else.
"""


def _from_gemini(m: Manifest, data: dict, profile) -> str:
    import os
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise ConfigError(
            "GEMINI_API_KEY is not set. Use --script-engine template for an "
            "offline run, or export the key."
        )
    try:
        import google.generativeai as genai
    except ImportError as exc:
        raise ConfigError(
            "google-generativeai is not installed. "
            "pip install google-generativeai, or use --script-engine template"
        ) from exc
    genai.configure(api_key=key)
    model = genai.GenerativeModel(os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    resp = model.generate_content(_prompt(m, data, profile))
    if not getattr(resp, "text", None):
        raise StageError("Gemini returned an empty response")
    return resp.text


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
