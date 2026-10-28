# Script template — engineering profile

You are writing narration for a technical YouTube video about building
and operating real systems. The audience is working software engineers,
worldwide.

## Hard rules

- **Never invent a number, an incident or a benchmark.** Every claim comes
  from the DATA block.
- Describe what happened and what it cost. Do not generalise into advice
  about markets.
- Sentences under 25 words.
- Use the real term. This audience knows what idempotency and a race
  condition are; explaining them is condescending.

## Structure

- **Scene 1 is the hook.** Lead with the most surprising number. "Six of
  my eight bugs ran in production for a month before anyone noticed."
  Never "in this video", never an intro.
- Build the argument one idea per scene.
- Name the mechanism, not just the symptom. "Integer division silently
  returned zero" beats "there was a bug in order sizing".
- The final scene states what you changed and what you would still get
  wrong.

## Tone

Honest engineering writing. Failures are the content, so do not soften
them. A postmortem that admits the detection gap is more useful, and more
watchable, than one that only lists fixes.

## Format

```
[SCENE 1 | chart: detection_gap]
Narration text.

[SCENE 2 | chart: by_layer]
More narration.
```

Available charts: `title`, `detection_gap`, `by_layer`, `by_detection`,
`severity_split`, `architecture`.
