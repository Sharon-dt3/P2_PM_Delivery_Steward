"""Who a commit belongs to.

The tracker (items, commitments) and the code host (commits) are separate
systems and need not name a person the same way -- a commit may be authored as
"wchen@acme.example" for the person the tracker knows as "wei.chen" -- and a
code host also holds automated accounts (CI, dependency bots) that are not
teammates. An IdentityMap says so explicitly, in a small JSON file:

    {"aliases": {"wchen@acme.example": "wei.chen"},
     "ignored_authors": ["ci-bot", "dependabot[bot]"]}

It is resolved when facts count commits, never guessed: an author that is
neither an alias, an ignored account nor a roster id (ignoring case) is taken
at face value, so a typo shows up as a new name instead of being merged into
the wrong person. validate() rejects maps that would merge or hide real
teammates by mistake.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from pm.people import Person, Resolution, resolve

DEFAULT_IDENTITIES_PATH = Path("data/identities.json")


class IdentityError(ValueError):
    """The identity map is malformed or contradicts the roster."""


def _norm(name: str) -> str:
    return name.strip().lower()


class IdentityMap(BaseModel):
    aliases: dict[str, str] = {}  # author name as the code host spells it -> canonical (roster) id
    ignored_authors: list[str] = []  # automated accounts whose commits are not a teammate's activity

    def resolution(self, author_id: str, people: Iterable[Person]) -> Resolution:
        """The full answer for a commit author, by the one rule in pm.people: a PERSON only when the author is exact (an alias, an id, or one
        person's exact display name); AMBIGUOUS when it could be more than one person; UNKNOWN when it is nobody's, even if it resembles someone."""
        return resolve(author_id, people, aliases=self.aliases, ignored=self.ignored_authors)

    def resolve(self, author_id: str, roster_ids: Iterable[str]) -> str | None:
        """The canonical person for a commit author, or None for an ignored
        automated account. Ids and aliases only: it cannot say AMBIGUOUS, so the facts use resolution() with the roster's names instead."""
        key = _norm(author_id)
        if key in {_norm(a) for a in self.ignored_authors}:
            return None
        aliases = {_norm(alias): target for alias, target in self.aliases.items()}
        if key in aliases:
            return aliases[key]
        by_lower = {_norm(r): r for r in roster_ids}
        return by_lower.get(key, author_id.strip())

    def validate(self, roster_ids: Iterable[str]) -> None:
        roster = {_norm(r): r for r in roster_ids}
        ignored = {_norm(a) for a in self.ignored_authors}
        for member in sorted(ignored & roster.keys()):
            raise IdentityError(f"{roster[member]} is on the roster and cannot also be an ignored author")
        seen: dict[str, str] = {}
        for alias, target in self.aliases.items():
            key = _norm(alias)
            if key in ignored:
                raise IdentityError(f"{key} is both an alias and an ignored author")
            if _norm(target) in ignored:
                raise IdentityError(f"alias {alias} points at ignored author {target}")
            if key in roster and _norm(roster[key]) != _norm(target):
                raise IdentityError(f"alias {alias} is the roster id {roster[key]}, but maps to {target}")
            if seen.setdefault(key, target) != target:
                raise IdentityError(f"alias {alias} maps to both {seen[key]} and {target}")


def load_identities(path: str | Path = DEFAULT_IDENTITIES_PATH) -> IdentityMap:
    """The identity map in `path`; an empty map when there is no file. A file
    that exists but is malformed raises IdentityError rather than quietly
    behaving as if it were empty."""
    path = Path(path)
    if not path.exists():
        return IdentityMap()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise TypeError("top level must be an object")
        aliases = data.get("aliases", {})
        ignored = data.get("ignored_authors", [])
        if not isinstance(aliases, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()):
            raise TypeError('"aliases" must map text to text')
        if not isinstance(ignored, list) or not all(isinstance(a, str) for a in ignored):
            raise TypeError('"ignored_authors" must be a list of text')
    except (ValueError, TypeError) as exc:
        raise IdentityError(f"{path.name} is not a valid identity map: {exc}") from exc
    return IdentityMap(aliases=aliases, ignored_authors=ignored)
