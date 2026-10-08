"""A channel brief can show the project's real git commits for its day, off unless repositories are configured for the channel.

A commit names a work item only by a tracker id; whether that item exists is checked against the tracker. Reading is read-only, and an unreadable
repository leaves the sections out and says so, rather than taking the brief down.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path


from pm.adapters.tracker import TrackerItem, TrackerMock
from pm.channel.record import record_file
from pm.channelbrief.commits import ENV_REPOS, commit_lines, repos_for
from pm.channelbrief.facts import MORNING, compute_channel_brief_facts
from pm.channelbrief.render import evidence_lines, render_content, render_message

CHANNEL = "19:real-channel@thread.tacv2"
NAME = "real-channel"
TZ = "Asia/Colombo"


def _git(repo: Path, *args: str, when: str | None = None, author: str = "Ada Lovelace") -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "a@example.test", "GIT_COMMITTER_NAME": author,
           "GIT_COMMITTER_EMAIL": "a@example.test", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def make_repo(path: Path, commits: list[tuple[str, str]]) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    for i, (message, when) in enumerate(commits):
        (path / f"f{i}").write_text(message)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", message, when=when)
    return path


def write_record(root: Path, day: str) -> None:
    path = record_file(root, CHANNEL, date.fromisoformat(day))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": "1.0", "channel_id": CHANNEL, "channel_display_name": NAME, "date": day, "allowlisted": True, "roster": [],
        "generated_at": f"{day}T12:00:00+00:00", "updates": [{"message_id": "m1", "text": "An update.", "quote": None}], "blockers": [],
        "decisions": [], "questions": [], "participation": [],
    }), encoding="utf-8")


def facts(db, tmp_path, repos, kind=MORNING, day=date(2026, 10, 9)):
    write_record(tmp_path / "out", "2026-10-08")
    return compute_channel_brief_facts(CHANNEL, NAME, kind, day, db_path=db, outcomes_dir=tmp_path / "out", repos=repos, timezone=TZ)


def test_repositories_are_configured_per_channel_by_name_or_id_and_default_to_none(monkeypatch):
    monkeypatch.setenv(ENV_REPOS, "Real-Channel=/a/one, /a/two ; other=/b/x;19:id-channel@thread.tacv2=/c/y")

    assert repos_for("real-channel", CHANNEL) == [Path("/a/one"), Path("/a/two")]  # the name, ignoring case
    assert repos_for("x", "19:id-channel@thread.tacv2") == [Path("/c/y")]  # or the id
    assert repos_for("nobody", "19:none") == []
    monkeypatch.delenv(ENV_REPOS)
    assert repos_for("real-channel", CHANNEL) == []


def test_commits_of_the_channels_calendar_day_are_listed_in_its_timezone(seeded_db_path, tmp_path):
    repo = make_repo(tmp_path / "r", [
        ("just after midnight", "2026-10-07T20:00:00+00:00"),  # 01:30 on the 8th in Colombo: this IS the 8th there
        ("late on the 8th", "2026-10-08T18:00:00+00:00"),  # 23:30 on the 8th in Colombo
        ("after the day", "2026-10-08T19:00:00+00:00"),  # 00:30 on the 9th in Colombo: not the 8th
        ("long before", "2026-10-05T09:00:00+00:00"),
    ])

    lines, state = commit_lines([repo], day=date(2026, 10, 8), timezone=TZ, db_path=seeded_db_path)

    assert state == "ok" and [c.subject for c in lines] == ["just after midnight", "late on the 8th"]
    assert all(c.day == "2026-10-08" and len(c.sha) == 7 and c.author == "Ada Lovelace" for c in lines)


def test_a_commit_naming_a_real_item_is_linked_one_naming_a_missing_item_says_so_and_a_plan_row_is_not_an_item(seeded_db_path, tmp_path):
    TrackerMock(db_path=seeded_db_path).create_item(TrackerItem(id="PM-031", title="Real", status="blocked", sprint_id="sprint-13", created_at="2026-10-07"))
    repo = make_repo(tmp_path / "r", [
        ("PM-031: fix the thing", "2026-10-08T09:00:00+00:00"), ("PM-999: names nothing real", "2026-10-08T10:00:00+00:00"),
        ("PM-29: a plan row", "2026-10-08T11:00:00+00:00"), ("no reference at all", "2026-10-08T12:00:00+00:00"),
    ])

    lines, _ = commit_lines([repo], day=date(2026, 10, 8), timezone=TZ, db_path=seeded_db_path)

    assert [(c.item_ref, c.item_known) for c in lines] == [("PM-031", True), ("PM-999", False), (None, None), (None, None)]


def test_the_brief_has_two_commit_sections_with_what_each_commit_says_and_names(seeded_db_path, tmp_path):
    TrackerMock(db_path=seeded_db_path).create_item(TrackerItem(id="PM-031", title="Real", status="blocked", sprint_id="sprint-13", created_at="2026-10-07"))
    repo = make_repo(tmp_path / "r", [("PM-031: fix the thing", "2026-10-08T09:00:00+00:00"), ("PM-999: ghost", "2026-10-08T10:00:00+00:00"),
                                      ("chore: bump the CI image", "2026-10-08T11:00:00+00:00")])

    content = render_content(facts(seeded_db_path, tmp_path, [repo]))

    assert "Commits are read from the project's git history, as written." in content
    assert "## Commits with no item reference (1)\n- " in content and ": chore: bump the CI image (Ada Lovelace, 2026-10-08)" in content
    assert "## Commits that name an item (2)" in content
    assert ": PM-031: fix the thing (Ada Lovelace, 2026-10-08; names PM-031)" in content
    assert ": PM-999: ghost (Ada Lovelace, 2026-10-08; names PM-999, which is not in the tracker)" in content


def test_with_no_repositories_configured_the_brief_has_no_commit_sections_at_all(seeded_db_path, tmp_path):
    result = facts(seeded_db_path, tmp_path, None)

    content = render_content(result)

    assert result.commits == () and result.sources["commits"] == "off"
    assert "Commits" not in content and "git history" not in content


def test_an_unreadable_repository_leaves_the_sections_out_and_the_audit_says_so(seeded_db_path, tmp_path):
    plain = tmp_path / "not_a_repo"
    plain.mkdir()

    result = facts(seeded_db_path, tmp_path, [plain])

    assert result.commits == () and result.sources["commits"] == "unreadable" and "## Updates" in render_content(result)  # the brief still stands


def test_a_long_commit_list_is_cut_to_the_limit_with_a_count_and_every_commit_is_in_the_evidence(seeded_db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PM_CHANNEL_BRIEF_LINES", "2")
    repo = make_repo(tmp_path / "r", [(f"change {n}", f"2026-10-08T0{n}:00:00+00:00") for n in range(1, 5)])
    result = facts(seeded_db_path, tmp_path, [repo])

    content = render_content(result)

    assert "## Commits with no item reference (4)" in content and "- and 2 more" in content.split("## Commits with no item reference")[1]
    assert [e["text"][9:] for e in evidence_lines(result) if e["section"] == "commit:unreferenced"] == ["change 1", "change 2", "change 3", "change 4"]
    assert "Commits" in render_message(result)


def test_the_job_reads_the_channels_configured_repositories_and_the_posted_proposal_carries_the_commits(seeded_db_path, tmp_path, monkeypatch):
    from datetime import datetime, time, timezone

    from spine.approval.proposals import ProposalStore

    from pm.jobs import channel_brief_job as job
    from pm.scheduling.config import ProjectScheduleConfig

    repo = make_repo(tmp_path / "r", [("chore: bump the CI image", "2026-10-08T09:00:00+00:00")])
    monkeypatch.setenv(ENV_REPOS, f"{NAME}={repo}")
    write_record(tmp_path / "out", "2026-10-08")
    config = ProjectScheduleConfig(channel_id=CHANNEL, timezone=TZ, working_days=["Mon", "Tue", "Wed", "Thu", "Fri"], morning_brief_time=time(8, 0),
                                   end_of_day_time=time(17, 45))
    moment = datetime(2026, 10, 9, 2, 30, tzinfo=timezone.utc)  # 08:00 Friday in Colombo: the morning brief reports Thursday the 8th

    result = job.run_channel_brief_job(config, NAME, MORNING, moment=moment, db_path=seeded_db_path, outcomes_dir=tmp_path / "out",
                                       publisher=None, policy=None)

    content = ProposalStore(seeded_db_path).get(result.proposal_id).payload["content"]
    assert "chore: bump the CI image (Ada Lovelace, 2026-10-08)" in content
