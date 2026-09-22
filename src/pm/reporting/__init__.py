"""PM-08's reporting workstream: facts.py computes deterministic facts
from a ProjectSnapshot (zero import of spine.llm/spine.prompts anywhere in
it -- see that module's own docstring for why); morning_brief.py turns
those facts into prose through a schema-constrained model call. Facts in
code, prose from the model -- the same split P1 itself uses for its own
daily/weekly summaries (packages/spine/src/spine/llm, .../prompts)."""

from __future__ import annotations
