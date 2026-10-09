"""Who is this? The one rule that decides which person an id, a name or an author string belongs to (PM-32).

A person is their ID. A display name is a label for an id, never the other way round: this is P1's rule too (a member is its Graph/AAD id, and
`resolve_display_name(member_id)` only reads the label), and where the two agents must meet by name (the shared nudge cap) they match on the same key:
the display name, lower-cased and trimmed (`lower(trim(display_name))`). Keeping one rule is what keeps the two agents agreeing about who someone is.

A reference to a person (a commit author, a name someone typed) resolves to a PERSON only when it is EXACT:

  1. an ignored account (a CI or dependency bot)                       -> IGNORED
  2. an explicit alias, written down in the identity map                -> PERSON
  3. a person's id, ignoring case                                       -> PERSON
  4. a person's display name, by the key above, when exactly one has it  -> PERSON; when two people have that exact name -> AMBIGUOUS

Anything else is NOT resolved, however close it looks. A string that merely *resembles* people (a first name, an initial, a prefix, a typo) is never
merged into one of them, because two people with similar names are exactly the case where a plausible guess is wrong half the time:

  * it resembles two or more people  -> AMBIGUOUS, with the candidates named: "'Olivia' could be Olivia Dupont or Olivia Dupree"
  * it resembles one person          -> UNKNOWN, with that person named as the closest match, and still not merged
  * it resembles nobody              -> UNKNOWN

AMBIGUOUS and UNKNOWN are counted for nobody and said as what they are. No code path in P2 turns a resemblance into an identity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher

PERSON, AMBIGUOUS, UNKNOWN, IGNORED = "person", "ambiguous", "unknown", "ignored"
TYPO_RATIO = 0.85  # how alike two whole names must be to count as "resembles" when the words do not line up (a typo): it only ever surfaces, never merges
_WORDS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Person:
    id: str
    name: str


@dataclass(frozen=True)
class Resolution:
    kind: str  # PERSON | AMBIGUOUS | UNKNOWN | IGNORED
    person_id: str | None = None  # set only for PERSON
    candidates: tuple[str, ...] = ()  # ids: who an AMBIGUOUS reference could be, or the one closest match of an UNKNOWN one
    reason: str = ""


def name_key(text: str) -> str:
    """The exact-match key for a name or an id: lower-cased and trimmed. The same key P1's shared-cap matching uses (`lower(trim(display_name))`)."""
    return (text or "").strip().lower()


def _words(text: str) -> list[str]:
    local = (text or "").split("@", 1)[0]  # an email address names its person by the part before the @
    return _WORDS.findall(local.lower())


def resembles(reference: str, person: Person) -> bool:
    """Whether `reference` looks like it could mean `person`. Only ever used to say "this could be them", never to decide that it is.

    Every word of the reference must line up with a different word of the person's name or id (an initial matches a word that starts with it, a longer
    word matches when it is a prefix of theirs), or the two whole names must be nearly the same text (a typo)."""
    ref = _words(reference)
    if not ref:
        return False
    theirs = _words(person.name) + [w for w in _words(person.id) if w not in _words(person.name)]
    free = list(theirs)
    lined_up = True
    for word in ref:
        hit = next((t for t in free if t.startswith(word)), None)
        if hit is None:
            lined_up = False
            break
        free.remove(hit)
    if lined_up:
        return True
    return len(ref) > 1 and SequenceMatcher(None, " ".join(ref), " ".join(_words(person.name))).ratio() >= TYPO_RATIO


def resolve(
    reference: str, people: Iterable[Person], *, aliases: dict[str, str] | None = None, ignored: Iterable[str] = (),
) -> Resolution:
    """The one rule. See the module docstring."""
    roster = list(people)
    key = name_key(reference)
    if key in {name_key(i) for i in ignored}:
        return Resolution(IGNORED, reason="an ignored automated account")
    explicit = {name_key(alias): target for alias, target in (aliases or {}).items()}
    if key in explicit:
        target = explicit[key]
        by_id = {name_key(p.id): p.id for p in roster}
        if name_key(target) in by_id:
            return Resolution(PERSON, person_id=by_id[name_key(target)], reason="an explicit alias")
        return Resolution(UNKNOWN, reason=f"an alias for {target!r}, who is not on the roster")
    by_id = {name_key(p.id): p.id for p in roster}
    if key in by_id:
        return Resolution(PERSON, person_id=by_id[key], reason="the person's id")
    same_name = [p for p in roster if name_key(p.name) == key and key]
    if len(same_name) == 1:
        return Resolution(PERSON, person_id=same_name[0].id, reason="the person's exact display name")
    if len(same_name) > 1:
        return Resolution(AMBIGUOUS, candidates=tuple(sorted(p.id for p in same_name)), reason="more than one person has exactly this name")
    like = tuple(sorted(p.id for p in roster if resembles(reference, p)))
    if len(like) > 1:
        return Resolution(AMBIGUOUS, candidates=like, reason="it could be any of them, and nothing says which")
    if len(like) == 1:
        return Resolution(UNKNOWN, candidates=like, reason="it resembles one person but is not their id, name or an alias, so it is not merged into them")
    return Resolution(UNKNOWN, reason="it matches nobody")


def label_for(person_id: str, people: Iterable[Person]) -> str:
    """A person as a heading or a sentence names them: their name, and their id too when another person has exactly the same name."""
    roster = list(people)
    mine = next((p for p in roster if p.id == person_id), None)
    if mine is None:
        return person_id
    twins = [p for p in roster if name_key(p.name) == name_key(mine.name)]
    return mine.name if len(twins) == 1 else f"{mine.name} ({mine.id})"


def labels(people: Iterable[Person]) -> dict[str, str]:
    roster = list(people)
    return {p.id: label_for(p.id, roster) for p in roster}
