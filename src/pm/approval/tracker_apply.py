"""Applying an approved tracker-changes batch: what gets written to the tracker, decided in code.

The batch comes from P1's channel outcome record (PM-26, pm.channel.batches) and holds two kinds of item. Approving it writes them
through the tracker adapter, after the approval gate has said yes. Nothing here asks a model anything and nothing here is a surface:
the service calls it, so the Teams card, the command line and the dashboard write the same things.

  comment  a comment on an item the channel line names, with the line itself, tagged `from-channel` and `ai-created`
  create   a new item for a blocker that names none: status blocked, NO assignee (never guessed), the sprint that covers the day (or, when
           none does, the sprint the operator NAMED in PM_TRACKER_DEFAULT_SPRINT; never one the agent picks), dated
           as of the channel message, `source_message_id` the channel message it came from, and a first comment saying where it came
           from, tagged `ai-created` -- so an item the agent made is never mistaken for one a person did

Idempotent: re-approving, a retry after a failed write, or a line already in the tracker never writes it twice. A comment is skipped when
the item already has one with the same words; an item is skipped when an item already traces back to that channel message (and its
"where it came from" comment is added if only that is missing). Anything that cannot be written is SKIPPED AND RECORDED with the reason, never
dropped silently: a comment on an item the tracker no longer has, a new item on a day no sprint covers and no default sprint is configured (an
item needs a sprint). A
batch where nothing at all can be written is refused BEFORE approval, so nothing is left approved-but-not-applied.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from spine.approval.proposals import Proposal

from pm.adapters.tracker import Tracker, TrackerItem
from pm.channel.batches import CHANNEL_TRACKER_PROPOSAL_TYPE

TRACKER_WRITE_TYPES = frozenset({CHANNEL_TRACKER_PROPOSAL_TYPE})
FROM_CHANNEL, AI_CREATED = "from-channel", "ai-created"
DEFAULT_SPRINT_ENV = "PM_TRACKER_DEFAULT_SPRINT"

_NUMBER = re.compile(r"^PM-(\d+)$")


class TrackerApplyRefused(ValueError):
    """The batch cannot be written to the tracker as it stands; the message says why in words."""


@dataclass(frozen=True)
class TrackerOp:
    kind: str  # create | comment
    item_id: str
    body: str | None = None  # the comment, for either kind (a created item's is its "where it came from" comment)
    tags: tuple[str, ...] = ()
    item: TrackerItem | None = None  # the new item, for a create


@dataclass(frozen=True)
class TrackerWrite:
    ops: tuple[TrackerOp, ...]
    skipped: tuple[dict, ...]  # items not written, each with the message it came from and why
    default_sprint: str | None = None  # the configured sprint, when any new item was placed in it because none covers its day

    @property
    def created(self) -> list[str]:
        return [op.item_id for op in self.ops if op.kind == "create"]

    @property
    def commented(self) -> list[str]:
        return [op.item_id for op in self.ops if op.kind == "comment"]

    @property
    def touched(self) -> list[str]:
        return sorted({op.item_id for op in self.ops})


def _next_number(items: list[TrackerItem]) -> int:
    return max((int(m.group(1)) for i in items if (m := _NUMBER.match(i.id))), default=0) + 1


def _provenance_prefix(ref: dict) -> str:
    return f"Created by the PM agent from {ref['channel_display_name']} message {ref['message_id']} on"


def _provenance(item: dict) -> str:
    ref = item["reference"]
    return f"{_provenance_prefix(ref)} {ref['date']}: {item['source_text']}"


def plan_writes(proposal: Proposal, *, tracker: Tracker) -> TrackerWrite:
    """What approving this batch writes, without writing it. Raises TrackerApplyRefused when nothing can be written."""
    if proposal.type not in TRACKER_WRITE_TYPES:
        raise TrackerApplyRefused(f"{proposal.type} is not a tracker-changes proposal")
    existing = tracker.list_items()
    ids = {i.id for i in existing}
    by_source = {i.source_message_id: i for i in existing if i.source_message_id}
    number = _next_number(existing)
    sprints = {s.id for s in tracker.list_sprints()}
    default = (os.environ.get(DEFAULT_SPRINT_ENV) or "").strip() or None
    used_default = False
    ops: list[TrackerOp] = []
    skipped: list[dict] = []

    for item in proposal.payload.get("items", []):
        ref = item["reference"]
        where = {"channel": ref["channel_display_name"], "message_id": ref["message_id"], "kind": item["kind"]}
        if item["kind"] == "comment":
            item_id = item["item_id"]
            if item_id not in ids:
                skipped.append({**where, "item_id": item_id, "reason": f"{item_id} is no longer in the tracker"})
                continue
            if any(c.body == item["body"] for c in tracker.list_comments(item_id)):
                skipped.append({**where, "item_id": item_id, "reason": f"{item_id} already has this comment"})
                continue
            ops.append(TrackerOp("comment", item_id, item["body"], (FROM_CHANNEL, *item.get("tags", []), AI_CREATED)))
        elif item["kind"] == "create":
            made = by_source.get(ref["message_id"])
            if made is not None:  # already created from this message (an earlier approval, or a retry): only the "where from" note may be missing,
                # and a reworded line does not get a second one: the note says where the item came from, and that has not changed
                if any(c.body.startswith(_provenance_prefix(ref)) for c in tracker.list_comments(made.id)):
                    skipped.append({**where, "item_id": made.id, "reason": f"{made.id} was already created from this message"})
                else:
                    ops.append(TrackerOp("comment", made.id, _provenance(item), (FROM_CHANNEL, AI_CREATED)))
                continue
            sprint = item.get("sprint_id")
            if not sprint:
                if default and default in sprints:
                    sprint, used_default = default, True
                elif default:
                    skipped.append({**where, "reason": f"no sprint covers {ref['date']}, and the configured default sprint {default!r} is not in the tracker"})
                    continue
                else:
                    skipped.append({**where, "reason": f"no sprint covers {ref['date']}, and an item needs a sprint "
                                                       f"(set {DEFAULT_SPRINT_ENV} to say where such items go)"})
                    continue
            new_id = f"PM-{number:03d}"
            number += 1
            new = TrackerItem(
                id=new_id, title=item["title"], status=item["status"], sprint_id=sprint, assignee_id=item.get("assignee_id"),
                created_at=ref["date"], blocked_since=ref["date"] if item["status"] == "blocked" else None, source_message_id=ref["message_id"],
            )
            ops.append(TrackerOp("create", new_id, _provenance(item), (FROM_CHANNEL, AI_CREATED), new))
        else:
            skipped.append({**where, "reason": f"an item of kind {item['kind']!r} is not one this writer knows"})

    if not ops:
        why = "; ".join(s["reason"] for s in skipped)
        raise TrackerApplyRefused(f"nothing to write: every item is already in the tracker or cannot be written ({why})" if skipped
                                  else "nothing to write: the batch is empty")
    return TrackerWrite(tuple(ops), tuple(skipped), default if used_default else None)


def write(plan: TrackerWrite, tracker: Tracker) -> None:
    """Carry the plan out, in order. A failure part-way leaves what was written; planning again skips it, so a retry finishes the rest."""
    for op in plan.ops:
        if op.kind == "create":
            tracker.create_item(op.item)
        tracker.add_comment(op.item_id, op.body, list(op.tags))
