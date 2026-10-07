"""PM-21: the prompts P2 sends to a model, by the job each does, and how the eval records them.

Every prompt lives in its own versioned file under prompts/<capability>/vN.md and is loaded through
spine's PromptRegistry; none is a string in code. Three of them are the ones the agent's wording
depends on, named here by what they do:

  brief       the morning stand-up brief                          pm08_morning_brief
  summary     the end-of-day summary (built on by PM-22)          pm22_end_of_day_summary
  mitigation  the drafted mitigation on a promoted risk (PM-19)   pm19_risk_promotion

(The mitigation prompt also asks for the entry's description and impact, which the same
evidence supports; the mitigation line is the part PM-19 adds.)

Every eval run records, next to its numbers, which version of each prompt produced them
(`prompt_versions` for every prompt in the registry, `prompt_roles` for these three) and a short
fingerprint of each prompt's text (`prompt_hashes`), so a prompt edited without a new version
number still shows up as a change in the history.
"""

from __future__ import annotations

import hashlib

from spine.prompts.registry import PromptRegistry

from pm.reporting.morning_brief import MORNING_BRIEF_CAPABILITY
from pm.risk.proposals import PROMOTION_CAPABILITY

SUMMARY_CAPABILITY = "pm22_end_of_day_summary"

BRIEF, SUMMARY, MITIGATION = "brief", "summary", "mitigation"
PROMPT_ROLES = {BRIEF: MORNING_BRIEF_CAPABILITY, SUMMARY: SUMMARY_CAPABILITY, MITIGATION: PROMOTION_CAPABILITY}


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def prompt_record(registry: PromptRegistry) -> dict:
    """What an eval run records about the prompts, from the registry as it is now."""
    roles = {}
    for role, capability in PROMPT_ROLES.items():
        prompt = registry.get(capability)
        roles[role] = {"capability": capability, "version": prompt.version, "sha256": fingerprint(prompt.text)}
    hashes = {name: fingerprint(registry.get(name).text) for name in registry.list_capabilities()}
    return {"prompt_roles": roles, "prompt_hashes": hashes}


def stale_prompts(record: dict, registry: PromptRegistry) -> list[str]:
    """Why a recorded run no longer describes the prompts in the registry, in words (empty: it does).
    A missing role, a different version, or the same version with different text."""
    problems = []
    roles = record.get("prompt_roles") or {}
    for role, capability in PROMPT_ROLES.items():
        if role not in roles:
            problems.append(f"the {role} prompt is not recorded")
            continue
        recorded, current = roles[role], registry.get(capability)
        if recorded.get("capability") != capability:
            problems.append(f"the {role} prompt was recorded as {recorded.get('capability')}, it is {capability}")
        elif recorded.get("version") != current.version:
            problems.append(f"the {role} prompt is {current.version} now, the run recorded {recorded.get('version')}")
        elif recorded.get("sha256") != fingerprint(current.text):
            problems.append(f"the {role} prompt {current.version} has been edited since the run without a new version")
    return problems


def format_prompts(registry: PromptRegistry) -> str:
    record = prompt_record(registry)
    lines = ["Prompts (versioned files in prompts/, loaded through the registry; recorded with every eval run)"]
    for role, info in record["prompt_roles"].items():
        lines.append(f"  {role:<11} {info['capability']:<26} {info['version']:<4} sha256 {info['sha256']}")
    return "\n".join(lines)
