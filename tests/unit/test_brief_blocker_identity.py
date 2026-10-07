"""Every blocker line in the morning brief says which risk it is and how severe it is, whatever the wording.

Found by golden case 9 on a real model: Claude wrote "Billing sync nightly job is at risk of missing SLA." with no
RISK-002 and no [high], so a reader could not tell which risk it was or how bad. The ranking was right (code sorts
it) but the identity and severity were lost to the wording. The renderer now puts them in when the model leaves
them out, once, and leaves a line that already carries them alone.
"""

from __future__ import annotations

import re

from pm.eval.pm12_cases import ScriptedGateway
from pm.eval.pristine import build_pristine_database
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import generate_morning_brief
from pm.state.snapshot import build_current_snapshot


def _blocker_lines(content):
    return [line for line in content.split("## Blockers")[1].splitlines() if line.startswith("- ")]


def _brief(tmp_path, gateway):
    db = build_pristine_database(tmp_path)
    snapshot = build_current_snapshot(db, taken_at="2026-09-18T12:00:00+00:00", tz_name="Asia/Colombo")
    return generate_morning_brief(compute_morning_brief_facts(snapshot), gateway)


def _strip_identity(lines):
    """What a model might write: the sentence, with no risk id and no severity tag."""
    return [{**l, "text": re.sub(r"^\[\w+\] RISK-\d+: ", "", re.sub(r" \(item [^)]*\)", "", l["text"]))} for l in lines]


def test_a_blocker_line_the_model_wrote_without_its_risk_id_and_severity_gets_both(tmp_path):
    gateway = ScriptedGateway(persistent_tamper={"blockers": _strip_identity})

    brief = _brief(tmp_path, gateway)

    first, second = _blocker_lines(brief.content)
    assert first.startswith("- [high] RISK-002: ") and second.startswith("- [medium] RISK-001: ")  # still in the code's ranked order
    assert "Billing sync nightly job at risk of missing SLA" in first and not any(brief.dropped.values())  # the wording was accepted


def test_each_blocker_is_identified_exactly_once(tmp_path):
    brief = _brief(tmp_path, ScriptedGateway(persistent_tamper={"blockers": _strip_identity}))

    text = "\n".join(_blocker_lines(brief.content))
    assert text.count("RISK-002") == 1 and text.count("RISK-001") == 1 and text.count("[high]") == 1 and text.count("[medium]") == 1


def test_a_line_that_already_carries_its_risk_is_left_alone(tmp_path):
    brief = _brief(tmp_path, ScriptedGateway())

    first, _ = _blocker_lines(brief.content)
    assert first.count("RISK-002") == 1 and first.count("[high]") == 1 and not first.startswith("- [high] RISK-002: [high]")


def test_a_line_with_the_id_but_no_severity_gets_only_the_severity(tmp_path):
    def keep_id_drop_severity(lines):
        return [{**l, "text": l["text"].replace("[high] ", "").replace("[medium] ", "")} for l in lines]

    brief = _brief(tmp_path, ScriptedGateway(persistent_tamper={"blockers": keep_id_drop_severity}))

    first, second = _blocker_lines(brief.content)
    assert first.count("RISK-002") == 1 and "[high]" in first and second.count("RISK-001") == 1 and "[medium]" in second


def test_a_blocker_whose_line_was_dropped_still_shows_its_recorded_fact(tmp_path):
    """Unchanged: a dropped line is the recorded fact, which already names the risk and its severity."""
    brief = _brief(tmp_path, ScriptedGateway(persistent_tamper={"blockers": lambda lines: [{**l, "reference_id": None} for l in lines]}))

    first, second = _blocker_lines(brief.content)
    assert "[as recorded]" in first and first.count("RISK-002") == 1 and "[high]" in first and "RISK-001" in second
