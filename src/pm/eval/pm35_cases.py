"""PM-35: the edge-case and failure pass, measured by the fabrication probe.

Seven situations a real day produces and a demo day does not, each run through the real pipeline (a database, stored snapshots, the facts, the model, the
grounding, the rendering) and each checked for one thing above all: that no progress is reported which the snapshots do not support.

  empty day            a project with nothing in it, and a full project on a quiet day
  no-activity person   a roster member with no items, commits or commitments, and one with only a commit
  item that moved twice  blocked then done; blocked then back; done, reopened and done again; done and reopened
  malformed model output  not JSON, the wrong shape, nothing, invented references, cut off
  rate-limit path      the model unavailable for the whole run, and from the third call on
  unassigned item      done, blocked and open items that nobody owns
  commit with no item reference  one that hints at work, and one that names an item that is not there

The check is `unsupported_morning_progress` / `unsupported_eod_progress`: they read the RENDERED text and compare each claim with the snapshots alone, never
with the facts the report was made from (the facts are what is being checked, as well as the model). A claim is an item named as delivered, pending, blocked,
unmapped, unassigned, shipped or newly blocked, an owner, a status move, a count. Each must be what the snapshots say. They are written separately from the
production code on purpose, so they can disagree with it, which is the point of a probe.

Every scenario returns the problems it found; each is registered as a GC2 probe (`register_gc2_probe`), so a fabrication in any of them counts toward the one
headline number, which stays a hard zero.
"""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

from spine.llm.gateway import LLMGatewayError, LLMResponse

from pm.adapters.tracker import TrackerMock
from pm.eval.pm12_cases import ScriptedGateway, count_fabrications, register_gc2_probe
from pm.eval.pristine import build_pristine_database
from pm.reporting.end_of_day_facts import compute_end_of_day_facts
from pm.reporting.end_of_day_summary import generate_end_of_day_summary
from pm.reporting.facts import compute_morning_brief_facts
from pm.reporting.morning_brief import generate_morning_brief
from pm.reporting.scripted_summary import ScriptedSummaryGateway
from pm.scheduling.config import ProjectScheduleConfig
from pm.seed.build import CHANNEL_ID
from pm.state.diff import compute_delta
from pm.state.identities import IdentityMap
from pm.state.snapshot import UNMAPPED, ProjectSnapshot, build_current_snapshot
from pm.state.store import DuplicateSnapshotError, read_snapshot, save_snapshot

MORNING = datetime(2026, 9, 16, 2, 30, tzinfo=timezone.utc)  # Wed 08:00 in Colombo
EVENING = datetime(2026, 9, 16, 11, 30, tzinfo=timezone.utc)  # Wed 17:00 in Colombo
QUIET_FROM = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)  # after the last move in the seed: nothing changes between these two
QUIET_TO = datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)
_OPEN = frozenset({"backlog", "in_progress", "in_review"})
_ITEM_ID = re.compile(r"\bPM-\d{3,}\b")
_PARENS = re.compile(r"\([^()]*\)")
_QUOTED = re.compile(r'"[^"]*"')
_BUCKET = re.compile(r"^- (Committed|Delivered|Pending|Blocked|Unmapped): (.*)$")
_HEADING = re.compile(r"^(?P<name>.*?)(?: \((?P<id>[^()]+)\))?$")


# --- the independent checks: what the rendered text claims, against the snapshots alone -------------------------------------------------------


def _roster(snap: ProjectSnapshot) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for person in snap.roster:
        names.setdefault(person.display_name, set()).add(person.id)
    return names


def unsupported_morning_progress(content: str, snap: ProjectSnapshot) -> list[str]:
    """Every claim in a morning brief that `snap` does not support."""
    items = {item.id: item for item in snap.items}
    roster = _roster(snap)
    problems: list[str] = []
    claims = _without_commit_messages(content)  # a commit message may name anything; it is shown as the commit's words, not as a claim about an item
    for found in sorted(set(_ITEM_ID.findall(_QUOTED.sub("", claims)))):
        if found not in items:
            problems.append(f"the brief names {found}, which is not in the snapshot")
    problems += _commit_line_problems(content, snap)
    owners: set[str] = set()
    heading = ""
    for line in content.splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            match = _HEADING.match(heading)
            owners = {i for i in roster.get(match["name"], set()) if match["id"] in (None, i)} if match else set()
            continue
        if heading == "Nobody is assigned" and line.startswith("- "):
            problems += _nobody_line_problems(line, items)
            continue
        bucket = _BUCKET.match(line)
        if bucket is None or bucket[1] == "Committed":
            continue
        label, text = bucket[1], bucket[2]
        if label == "Unmapped":
            for item_id in _ITEM_ID.findall(_PARENS.sub("", text.split(":", 1)[0])):
                if item_id in items and items[item_id].status != UNMAPPED:
                    problems.append(f"{heading}: {item_id} is shown as unmapped, but the snapshot has it as {items[item_id].status}")
            continue
        for item_id in _ITEM_ID.findall(_PARENS.sub("", text)):
            item = items.get(item_id)
            if item is None:
                continue  # reported above
            allowed = {"Delivered": {"done"}, "Pending": _OPEN, "Blocked": {"blocked"}}[label]
            if item.status not in allowed:
                problems.append(f"{heading}: {item_id} is shown as {label.lower()}, but the snapshot has it as {item.status}")
            if owners and item.assignee_id not in owners:
                problems.append(f"{heading}: {item_id} is credited to them, but the snapshot has it assigned to {item.assignee_id}")
    problems += _sprint_line_problems(content, snap)
    return problems


def _without_commit_messages(content: str) -> str:
    kept, in_commits = [], False
    for line in content.splitlines():
        if line.startswith("## "):
            in_commits = line.strip() == "## Commits with no item reference"
        if not in_commits or line.startswith("## "):
            kept.append(line)
    return "\n".join(kept)


def _commit_line_problems(content: str, snap: ProjectSnapshot) -> list[str]:
    """Each commit shown as tied to no work must be a commit in the snapshot that really ties to no work."""
    by_sha = {commit.sha[:7]: commit for commit in snap.commits}
    problems, in_commits = [], False
    for line in content.splitlines():
        if line.startswith("## "):
            in_commits = line.strip() == "## Commits with no item reference"
            continue
        shown = re.match(r"- ([0-9a-f]{7}): ", line) if in_commits else None
        if shown is None:
            continue
        commit = by_sha.get(shown[1])
        if commit is None:
            problems.append(f"commit {shown[1]} is shown, but it is not in the snapshot")
        elif commit.item_ref is not None:
            problems.append(f"commit {shown[1]} is shown as tied to no work, but the snapshot ties it to {commit.item_ref}")
    return problems


def _nobody_line_problems(line: str, items: dict) -> list[str]:
    found = _ITEM_ID.findall(_PARENS.sub("", line))
    if not found or found[0] not in items:
        return []
    item = items[found[0]]
    problems = []
    if item.assignee_id is not None:
        problems.append(f"{item.id} is listed as unassigned, but the snapshot has it assigned to {item.assignee_id}")
    if item.status == "done":
        problems.append(f"{item.id} is listed as work nobody is assigned to, but the snapshot has it done")
    stated = re.search(r"status (\w+)", line)
    if stated and stated[1] != item.status:
        problems.append(f"{item.id} is shown with status {stated[1]}, but the snapshot has {item.status}")
    return problems


def _sprint_line_problems(content: str, snap: ProjectSnapshot) -> list[str]:
    match = re.search(r"\((sprint-[\w-]+)\).*?; (\d+) of (\d+) items in this sprint are done", content)
    if match is None:
        return []
    in_sprint = [item for item in snap.items if item.sprint_id == match[1]]
    done = sum(1 for item in in_sprint if item.status == "done")
    if (int(match[2]), int(match[3])) != (done, len(in_sprint)):
        return [f"the brief says {match[2]} of {match[3]} items in {match[1]} are done, but the snapshot has {done} of {len(in_sprint)}"]
    return []


def unsupported_eod_progress(content: str, morning: ProjectSnapshot, evening: ProjectSnapshot) -> list[str]:
    """Every claim in an end-of-day summary that the two snapshots do not support."""
    before = {item.id: item for item in morning.items}
    after = {item.id: item for item in evening.items}
    problems: list[str] = []
    section = ""
    mentioned: set[str] = set()
    for line in content.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if not line.startswith("- ") or line == "- none.":
            continue
        subject = _ITEM_ID.findall(_QUOTED.sub("", line))
        if not subject:
            continue
        item_id = subject[0]
        mentioned.add(item_id)
        if item_id not in before and item_id not in after:
            problems.append(f"the summary names {item_id}, which is in neither snapshot")
            continue
        now, was = after.get(item_id), before.get(item_id)
        if section == "What shipped" and not (now and now.status == "done" and not (was and was.status == "done")):
            problems.append(f"{item_id} is shown as shipped today, but the snapshots have it {was.status if was else 'absent'} -> {now.status if now else 'absent'}")
        if section == "What is still pending" and not (now and now.status in _OPEN and (was is None or was.status != now.status)):
            problems.append(f"{item_id} is shown as still pending, but the snapshots have it {was.status if was else 'absent'} -> {now.status if now else 'absent'}")
        if section == "What is newly blocked" and not (now and now.status == "blocked" and not (was and was.status == "blocked")):
            problems.append(f"{item_id} is shown as newly blocked, but the snapshots have it {was.status if was else 'absent'} -> {now.status if now else 'absent'}")
        move = re.search(r"moved from (\w+) to (\w+)", line)
        if move and (was is None or now is None or (move[1], move[2]) != (was.status, now.status)):
            problems.append(f"{item_id} is said to have moved {move[1]} -> {move[2]}, but the snapshots have {was.status if was else 'absent'} -> {now.status if now else 'absent'}")
    count = re.search(r"(\d+) other open items? did not change today", content)
    if count:
        unchanged = len({i for i, item in after.items() if item.status not in ("done", UNMAPPED)} - mentioned)  # blocked work is open work
        if int(count[1]) != unchanged:
            problems.append(f"the summary says {count[1]} other open items did not change, but the snapshots leave {unchanged}")
    return problems


# --- stand-ins for a model that is unavailable or wrong ---------------------------------------------------------------------------------------


def _reply(text: str) -> LLMResponse:
    return LLMResponse(text=text, provider="scripted", model="scripted", prompt_tokens=0, completion_tokens=0, latency_ms=0.0, cache_hit=False)


class UnavailableGateway:
    """A model that cannot be reached: every call raises what the real gateway raises once its rate-limit retries and its local fallback are spent."""

    def __init__(self, error: str = "bedrock rate-limited after 4 attempts") -> None:
        self._error = error
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        raise LLMGatewayError(self._error)


class UnavailableAfter:
    """Answers faithfully for `good_calls` calls, then is rate-limited for every call after."""

    def __init__(self, inner, good_calls: int) -> None:
        self._inner, self._good = inner, good_calls
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        if self.calls > self._good:
            raise LLMGatewayError("bedrock rate-limited after 4 attempts")
        return self._inner.generate(prompt, **kwargs)


class RawGateway:
    """A model whose every answer is exactly `text`, whatever it was asked."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls = 0

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.calls += 1
        return _reply(self._text)


MALFORMED_OUTPUTS = {
    "prose, not JSON": "I'm sorry, I can't produce that as JSON. Here is a summary: everything is on track.",
    "an empty list": "[]",
    "null": "null",
    "lines is a string": json.dumps({"lines": "everything shipped"}),
    "lines missing their fields": json.dumps({"lines": [{"text": "PM-001 shipped"}]}),
    "valid JSON, no lines": json.dumps({"lines": []}),
    "invented references": json.dumps({"lines": [{"text": "PM-999 shipped on time.", "reference_id": "item:PM-999", "quote": "shipped on time"}]}),
    "cut off mid-answer": '{"lines": [{"text": "PM-0',
}


# --- a real project to run the scenarios on ------------------------------------------------------------------------------------------------


def config() -> ProjectScheduleConfig:
    return ProjectScheduleConfig(channel_id=CHANNEL_ID, timezone="Asia/Colombo", working_days=["Mon", "Tue", "Wed", "Thu", "Fri"],
                                 morning_brief_time=time(8, 0), end_of_day_time=time(17, 0))


def fresh_db(directory: Path) -> Path:
    """The seeded project, built from the frozen seed (the original three risks, not the live risk log) in a file of its own."""
    return build_pristine_database(directory)


def capture(db: Path, moment: datetime) -> ProjectSnapshot:
    """The project as it stands at `moment`, stored, from the database alone: no live risk log, no identity file."""
    snapshot = build_current_snapshot(db_path=db, taken_at=moment.isoformat(), identities=IdentityMap(), tz_name=config().timezone)
    try:
        save_snapshot(snapshot, db_path=db)
    except DuplicateSnapshotError:
        snapshot = read_snapshot(moment.isoformat(), db_path=db)
    return snapshot


def add_item(db: Path, item_id: str, title: str, status: str, history: list[tuple[str, str, str]], assignee: str | None = "aisha.rahman") -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO items (id, title, status, sprint_id, assignee_id, created_at, blocked_since, source_message_id) "
                 "VALUES (?, ?, ?, 'sprint-13', ?, '2026-09-01', NULL, NULL)", (item_id, title, status, assignee))
    for frm, to, at in history:
        conn.execute("INSERT INTO item_transitions (item_id, from_status, to_status, changed_at) VALUES (?, ?, ?, ?)", (item_id, frm, to, at))
    conn.commit()
    conn.close()


def add_commit(db: Path, sha: str, author: str, message: str, item_ref: str | None, at: str = "2026-09-16T01:00:00+00:00") -> None:
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO commits (sha, author_id, message, item_ref, committed_at) VALUES (?, ?, ?, ?, ?)", (sha, author, message, item_ref, at))
    conn.commit()
    conn.close()


def empty_the_project(db: Path) -> None:
    """Nothing in it: no items, no transitions, no comments, no commits, no commitments, no risks. The roster and the sprint stay: it is still a project."""
    conn = sqlite3.connect(db)
    for table in ("commitments", "risks", "item_comments", "item_transitions", "commits", "items"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


@dataclass
class Day:
    """One working day run for real: the morning snapshot and brief, the evening snapshot and the summary of what changed between them."""

    morning: ProjectSnapshot
    evening: ProjectSnapshot
    brief: object
    summary: object
    brief_gateway: object
    summary_gateway: object


def run_day(db: Path, *, brief_gateway=None, summary_gateway=None, start: datetime = MORNING, end: datetime = EVENING) -> Day:
    brief_gateway = brief_gateway if brief_gateway is not None else ScriptedGateway()
    summary_gateway = summary_gateway if summary_gateway is not None else ScriptedSummaryGateway()
    morning = capture(db, start)
    evening = capture(db, end)
    brief = generate_morning_brief(compute_morning_brief_facts(morning), brief_gateway)
    delta = compute_delta(morning, evening, TrackerMock(db_path=db))
    summary = generate_end_of_day_summary(compute_end_of_day_facts(delta, morning, evening), summary_gateway)
    return Day(morning, evening, brief, summary, brief_gateway, summary_gateway)


def problems_in(day: Day, *, evening_brief: bool = False) -> list[str]:
    """Everything wrong with a run: the brief and the summary, each checked line by line against its facts (the GC2 probe) and claim by claim against
    the snapshots (this module's)."""
    found = [f"brief: {p}" for p in count_fabrications(day.brief, day.brief.facts)]
    found += [f"brief: {p}" for p in unsupported_morning_progress(day.brief.content, day.morning)]
    found += [f"summary: {p}" for p in unsupported_eod_progress(day.summary.content, day.morning, day.evening)]
    return found


# --- the seven scenarios --------------------------------------------------------------------------------------------------------------------


def scenario_empty_day() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        empty_the_project(db)
        brief_gateway = _PromptLog(ScriptedGateway())
        day = run_day(db, brief_gateway=brief_gateway)
        problems += [f"nothing in the project: {p}" for p in problems_in(day)]
        if day.summary_gateway.calls:
            problems.append("nothing in the project: the model was called to word changes in a project with nothing in it")
        if any("PM-" in prompt for prompt in brief_gateway.prompts):
            problems.append("nothing in the project: the model was given an item in a project with none")
        if _ITEM_ID.search(day.brief.content + day.summary.content):
            problems.append("nothing in the project: an item is named in a report about an empty project")
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        day = run_day(db, start=QUIET_FROM, end=QUIET_TO)  # a full project, and nothing moved between the two snapshots
        problems += [f"a quiet day: {p}" for p in problems_in(day)]
        if day.summary_gateway.calls:
            problems.append("a quiet day: the model was called to word changes that did not happen")
        if re.search(r"\bmoved from\b", day.summary.content.split("\n", 1)[1]):
            problems.append("a quiet day: the summary reports a move although nothing moved")
    return problems


def scenario_person_with_no_activity() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        add_commit(db, "ee00001", "wei.chen", "tidy the build scripts", None)
        conn = sqlite3.connect(db)
        conn.execute("INSERT OR IGNORE INTO assignees (id, display_name) VALUES ('quiet.new', 'Quinn Quiet')")
        conn.commit()
        conn.close()
        gateway = _PromptLog(ScriptedGateway())
        day = run_day(db, brief_gateway=gateway)
        problems += problems_in(day)
        block = day.brief.content.split("## Quinn Quiet", 1)
        if len(block) != 2 or block[1].split("\n## ", 1)[0].strip() != "- No update: no tracker activity or commits recorded.":
            problems.append("a person with nothing is not shown as having no update")
        if any("Quinn Quiet" in prompt or "quiet.new" in prompt for prompt in gateway.prompts):
            problems.append("the model was given a person who has no activity")
        if "Quinn Quiet" in day.summary.content:
            problems.append("a person with no activity appears in the end-of-day summary")
    return problems


def scenario_item_moved_twice() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        d = "2026-09-16T"
        add_item(db, "PM-301", "Blocked, then shipped", "done", [("in_progress", "blocked", d + "04:00:00+00:00"), ("blocked", "done", d + "09:00:00+00:00")])
        add_item(db, "PM-302", "Blocked and back again", "in_progress", [("in_progress", "blocked", d + "04:00:00+00:00"), ("blocked", "in_progress", d + "09:00:00+00:00")])
        add_item(db, "PM-303", "Done, reopened, done again", "done", [("in_progress", "done", "2026-09-15T09:00:00+00:00"),
                                                                       ("done", "in_progress", d + "03:00:00+00:00"), ("in_progress", "done", d + "10:00:00+00:00")])
        add_item(db, "PM-304", "Done, then reopened", "in_progress", [("in_progress", "done", d + "03:30:00+00:00"), ("done", "in_progress", d + "10:30:00+00:00")])
        day = run_day(db)
        problems += problems_in(day)
        for item_id in ("PM-301", "PM-302", "PM-303", "PM-304"):
            subjects = [line for line in day.summary.content.splitlines() if line.startswith("- ") and _ITEM_ID.findall(_QUOTED.sub("", line))[:1] == [item_id]]
            if len(subjects) > 1:
                problems.append(f"{item_id} moved twice but is reported {len(subjects)} times")
        shipped = day.summary.content.split("## What shipped", 1)[1].split("\n## ", 1)[0]
        if "PM-301" not in shipped:
            problems.append("PM-301 was blocked and then done within the day, and is not reported as shipped")
        for item_id, why in (("PM-302", "it ended where it began"), ("PM-303", "it was already done this morning"), ("PM-304", "it ended open")):
            if item_id in shipped:
                problems.append(f"{item_id} is reported as shipped, but {why}")
    return problems


def scenario_malformed_model_output() -> list[str]:
    problems: list[str] = []
    for name, text in MALFORMED_OUTPUTS.items():
        with tempfile.TemporaryDirectory() as tmp:
            db = fresh_db(Path(tmp))
            add_item(db, "PM-321", "Ship the export button", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")])
            day = run_day(db, brief_gateway=RawGateway(text), summary_gateway=RawGateway(text))
            problems += [f"{name}: {p}" for p in problems_in(day)]
            if "PM-999" in day.brief.content + day.summary.content:
                problems.append(f"{name}: an invented item reached the reports")
            if "PM-321" not in day.summary.content.split("## What shipped", 1)[1].split("\n## ", 1)[0]:
                problems.append(f"{name}: a change that really happened was lost with the model's answer")
            if "PM-009" not in day.brief.content:
                problems.append(f"{name}: a delivered item was lost from the brief with the model's answer")
    return problems


def scenario_rate_limit_path() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        add_item(db, "PM-331", "Ship the export button", "done", [("in_progress", "done", "2026-09-16T06:00:00+00:00")])
        add_item(db, "PM-332", "Wire the audit log", "blocked", [("in_progress", "blocked", "2026-09-16T07:00:00+00:00")])
        add_item(db, "PM-333", "Review the pricing page", "in_review", [("in_progress", "in_review", "2026-09-16T08:00:00+00:00")])  # three sections have facts
        down, down_summary = UnavailableGateway(), UnavailableGateway()
        day = run_day(db, brief_gateway=down, summary_gateway=down_summary)
        problems += [f"model unavailable: {p}" for p in problems_in(day)]
        if down.calls != 1 or down_summary.calls != 1:
            problems.append(f"model unavailable: the unavailable model was called {down.calls} and {down_summary.calls} times, not once each")
        if "PM-331" not in day.summary.content.split("## What shipped", 1)[1].split("\n## ", 1)[0] or "PM-009" not in day.brief.content:
            problems.append("model unavailable: the recorded facts were lost with the model")
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        day = run_day(db, brief_gateway=UnavailableAfter(ScriptedGateway(), 2))
        problems += [f"model unavailable from the third call: {p}" for p in problems_in(day)]
    return problems


def scenario_unassigned_item() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        d = "2026-09-16T"
        add_item(db, "PM-341", "Nobody's finished item", "done", [("in_progress", "done", d + "06:00:00+00:00")], assignee=None)
        add_item(db, "PM-342", "Nobody's blocked item", "blocked", [("in_progress", "blocked", d + "07:00:00+00:00")], assignee=None)
        add_item(db, "PM-343", "Nobody's open item", "in_progress", [("backlog", "in_progress", d + "08:00:00+00:00")], assignee=None)
        day = run_day(db)
        problems += problems_in(day)
        nobody = day.brief.content.split("## Nobody is assigned", 1)
        if len(nobody) != 2:
            problems.append("items nobody owns are not listed in a section of their own")
        else:
            listed = nobody[1].split("\n## ", 1)[0]
            for item_id in ("PM-342", "PM-343"):
                if item_id not in listed:
                    problems.append(f"{item_id} has no owner and is missing from the unassigned section")
        people_part = day.brief.content.split("## Nobody is assigned", 1)[0]
        for item_id in ("PM-341", "PM-342", "PM-343"):
            if item_id in people_part:
                problems.append(f"{item_id} has no owner but is credited to someone")
        owner_lines = [line for line in day.summary.content.splitlines() if re.match(r"- PM-34[123] ", line)]
        for line in owner_lines:
            owner = re.search(r"Owner: ([^.]*)\.", line)
            if owner and owner[1].strip() not in ("nobody", "unassigned", "no one", "none"):
                problems.append(f"an item nobody owns is shown with an owner: {line}")
    return problems


def scenario_commit_with_no_item_reference() -> list[str]:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        db = fresh_db(Path(tmp))
        add_commit(db, "ef00001", "wei.chen", "finished the export fix, ready to ship", None)
        add_commit(db, "ef00002", "noah.becker", "PM-999: wire up the new queue", None)  # names an item the tracker does not have
        before = {i.id: i.status for i in capture(db, QUIET_FROM).items}
        day = run_day(db, start=QUIET_FROM, end=QUIET_TO)
        problems += problems_in(day)
        for sha in ("ef00001", "ef00002"):
            if sha[:7] not in day.brief.content:
                problems.append(f"commit {sha} has no item reference and is not reported as a commit that ties to no work")
        if "PM-999" in day.summary.content:
            problems.append("a commit naming PM-999 made the summary name an item the tracker does not have")
        after = {i.id: i.status for i in day.evening.items}
        if before != after:
            problems.append("the commits changed an item's status")
        if "PM-020" in day.summary.content:
            problems.append("a commit message about the export made the summary report progress on an item")
    return problems


class _PromptLog:
    """Wraps a gateway and keeps every prompt it was given."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return self._inner.calls

    def generate(self, prompt: str, **kwargs: object) -> LLMResponse:
        self.prompts.append(prompt)
        return self._inner.generate(prompt, **kwargs)


def _guarded(scenario: Callable[[], list[str]]) -> Callable[[], list[str]]:
    """A scenario in which the pipeline raises has found a failure: a report that cannot be made is not a report that does not fabricate."""
    def run() -> list[str]:
        try:
            return scenario()
        except Exception as exc:  # noqa: BLE001 - whatever it is, the pass has to say so rather than die
            return [f"the pipeline raised {type(exc).__name__}: {exc}"]

    run.__name__ = scenario.__name__
    return run


SCENARIOS: dict[str, Callable[[], list[str]]] = {
    "edge: empty day": _guarded(scenario_empty_day),
    "edge: person with no activity": _guarded(scenario_person_with_no_activity),
    "edge: item that moved twice": _guarded(scenario_item_moved_twice),
    "edge: malformed model output": _guarded(scenario_malformed_model_output),
    "edge: rate-limit path": _guarded(scenario_rate_limit_path),
    "edge: unassigned item": _guarded(scenario_unassigned_item),
    "edge: commit with no item reference": _guarded(scenario_commit_with_no_item_reference),
}


def register() -> None:
    """Add the seven scenarios to GC2's probes, once however many times the harness is wired."""
    from pm.eval import pm12_cases

    known = {name for name, _ in pm12_cases.GC2_EXTRA_PROBES}
    for name, scenario in SCENARIOS.items():
        if name not in known:
            register_gc2_probe(name, scenario)
