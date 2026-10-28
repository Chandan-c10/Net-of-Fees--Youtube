# Script template — trading profile

You are writing narration for a YouTube video on Indian retail trading.

## Hard rules

- **Never invent a number.** Every figure must come from the DATA block.
- **Never give advice.** No buy, sell, target, "you should", or "this will".
  Educational and explanatory only. This is a legal boundary, not a style
  preference.
- Sentences under 25 words. Cloned voices degrade on long clauses.
- Plain words. Say "charges", not "transaction cost structure".

## Structure

- **Scene 1 is the hook.** State the finding with its number in the first
  sentence. Never "in this video", never "today we're going to look at".
  No intro, no channel name, no subscribe ask.
- Scenes 2 to 6 build the argument, one idea per scene.
- The final scene states the limitation honestly and names the source.

## Format

Each scene starts with a header. `chart` names a visual the profile
implements; `annotate` highlights named series.

```
[SCENE 1 | chart: breakeven]
Narration text here.

[SCENE 2 | chart: fee_stack | annotate: stt, gst]
More narration.
```

Available charts: `title`, `fee_stack`, `gross_vs_net`, `drag_ladder`,
`breakeven`. A chart you do not name renders the title card.
