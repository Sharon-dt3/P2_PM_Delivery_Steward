"""The words of a reminder and of an escalation. Fixed templates filled from the record: no model is involved, so what
a person is told about their own commitment is exactly what was recorded, never a paraphrase."""

from __future__ import annotations

from pm.commitments.store import TrackedCommitment


def _first_name(name: str | None) -> str:
    return name.split()[0] if name and "." not in name.split()[0] else ""


def _when(due: str, days_to_due: int) -> str:
    if days_to_due == 0:
        return "today"
    if days_to_due == 1:
        return "tomorrow"
    return f"on {due}" if days_to_due > 0 else f"on {due}, {-days_to_due} day{'s' if days_to_due < -1 else ''} ago"


def nudge_text(commitment: TrackedCommitment, *, owner_name: str | None, due: str, days_to_due: int) -> str:
    greeting = f"Hi {_first_name(owner_name)}," if _first_name(owner_name) else "Hi,"
    return (
        f'{greeting} a quick reminder about something you committed to on {commitment.made_at}: "{commitment.text}" '
        f"It is due {_when(due, days_to_due)}. If the date has moved or it is already done, a one-line reply is all we need. Thanks!"
    )


def escalation_text(
    commitment: TrackedCommitment, *, owner_name: str | None, due: str, overdue_days: int, threshold_days: int,
    nudge_day: str, item_status: str | None,
) -> str:
    owner = owner_name or commitment.member_id
    item = f" It is tied to {commitment.item_id}, which is {item_status}." if commitment.item_id and item_status else ""
    source = f" Source message: {commitment.source_message_id}." if commitment.source_message_id else ""
    return (
        f'Overdue commitment for the delivery lead: {owner} committed on {commitment.made_at} to: "{commitment.text}" '
        f"It was due {due} and is now {overdue_days} day{'s' if overdue_days != 1 else ''} overdue "
        f"(you asked to hear about anything overdue by more than {threshold_days} day{'s' if threshold_days != 1 else ''}). "
        f"{owner} was reminded on {nudge_day}.{item}{source} Nothing has been recorded against it since."
    )
