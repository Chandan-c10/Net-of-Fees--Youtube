# Voices

One directory per voice. Each must contain:

| File | Purpose |
| --- | --- |
| `consent.md` | **Required.** Who the voice belongs to and their permission. A voice without this fails validation and the episode will not render. |
| `meta.json` | Language, engine, model, notes |
| `lexicon.json` | Pronunciation overrides. Finance vocabulary is mispronounced by every engine tried, so this is required rather than optional |
| `reference/` | The source recordings used for cloning |

Cloning your own voice is yours to do. Any other voice needs the
speaker's documented permission -- architecture doc, section 14.
