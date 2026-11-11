"""Recipe: a story, which needs no data at all.

Every other recipe exists to freeze a slice of facts -- a trade's
charges, a log of real incidents -- so the narration can quote figures it
did not invent. A fable has no figures. Its whole content is the story,
and the story is written at the script stage by the story engine.

So this recipe reads nothing and queries nothing. It writes down which
story is being told and where the telling comes from, which is all a
snapshot of a fable can honestly contain.

It exists because the autopilot had to name *some* recipe, and the one
it named was `incident_log`. That recipe reads `data/incidents.json`,
which is gitignored and therefore does not exist on a runner, so every
unattended run died on its first command -- before the writing, the
pictures or the voice were reached. The fix is not to ship the data to
the runner. It is to stop asking a fable for data it does not have.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import register

# Panchatantra, Jataka, Mahabharata, Ramayana: all long out of copyright.
# The retelling is this channel's own words, which is what makes the
# episode original rather than a copy.
SOURCE = "public-domain folk tale, retold in the channel's own words"


@dataclass
class StoryRecipe:
    """One story. The snapshot records which one, and nothing else."""

    name: str = "story"
    tier: str = "A"
    title_template: str = "Once upon a time"
    needs_local_data: bool = False

    def export(self, **params: object) -> dict:
        tradition = str(params.get("tradition", "Panchatantra"))
        return {
            "kind": "story",
            "tradition": tradition,
            "source": SOURCE,
            # Narrated from the title downwards; there are no figures to
            # bind, and the picture carries the scene.
            "binds_figures": False,
        }


register(StoryRecipe())
