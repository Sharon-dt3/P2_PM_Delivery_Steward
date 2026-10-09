# Who is who: the similar-name guard (PM-32)

Two people with similar names are never merged, and an attribution that could be more than one person is said to be ambiguous. This is one rule, in one
module (`src/pm/people.py`), and it is the rule P1 already works by.

## The rule

**A person is their id. A name is a label.** A reference to a person (a commit author, a name someone typed) resolves to a person only when it is **exact**:

| Order | The reference is... | Result |
|---|---|---|
| 1 | an ignored automated account (CI, dependency bots) | ignored |
| 2 | an explicit alias written in the identity map | that person |
| 3 | a person's id, ignoring case and outer spaces | that person |
| 4 | one person's display name, **lower-cased and trimmed** | that person; if **two** people have that exact name: **ambiguous** |
| 5 | anything else that resembles **two or more** people (a first name, an initial, a prefix) | **ambiguous**, with the candidates named |
| 6 | anything else that resembles **one** person (`O. Dupont`, a typo) | **unknown**, with the closest match named, and **not merged** |
| 7 | nothing | unknown |

"Resembles" is only ever a hint, never an identity. Every word of the reference must line up with a different word of the person's name or id (an initial matches a word
that starts with it; a longer word matches when it is a prefix), or two multi-word names must be nearly the same text (a typo). No code path turns a resemblance into
an identity: `Olivia` is not Olivia Dupont and not Olivia Dupree, it is "ambiguous, it could be Olivia Dupont or Olivia Dupree".

## What the brief does with each outcome

- **A person** gets the credit (commits, items, commitments), under a heading of their own. `Olivia Dupont` and `Olivia Dupree` are two sections, always.
- **Ambiguous** is credited to **nobody** and listed in a code-worded section:
  `- 'Olivia' (2 commits): ambiguous, it could be Olivia Dupont or Olivia Dupree. Not counted for either.`
- **Unknown but resembling one person** keeps its own name (a typo shows up as a new name) and is listed with who it resembles:
  `- 'O. Dupont' (1 commit): not matched to anyone. It resembles Olivia Dupont but is not their id, name or an alias, so it is not merged into them.`
- A commit with no item reference by an ambiguous author says so in its own line: `Olivia (ambiguous: could be Olivia Dupont or Olivia Dupree)`.
- **Two people with exactly the same name** (ignoring case and outer spaces) are told apart by id in every heading and sentence: `Sam Lee (sam.one)`, `Sam Lee (sam.two)`.
  The channel brief does the same for authors it reads from P1's store (`Sam Lee (aad-0003)`).
- To make an author resolve, write the alias down (`data/identities.json`). Nothing else will.

## What is shared with P1

| | P1 | P2 |
|---|---|---|
| Identity | a member is its Graph/AAD id | a person is their id |
| A name | a label read from the id (`members_repo.resolve_display_name`) | a label (`pm.people.label_for`) |
| Matching by name across the two agents | `lower(trim(display_name)) = lower(trim(?))` in `p1/nudges/shared_cap.py` | the same expression in `pm/commitments/cap.py`; `pm.people.name_key` is its Python form |
| Two people with one name | both are counted against the cap (the safe side) | ambiguous; both are counted against the cap |
| Similar names | stay separate members | stay separate people, never merged |

`tests/unit/test_people_resolution.py` keeps that true: it checks `name_key` equals the SQL expression on a table of names, and that the exact expression appears in both agents' code,
so if either side changes how it matches names, a test says so.

**Known limit, for the spine hardening row (PM-40):** SQLite's `lower()` only lower-cases ASCII, so two spellings of a name that differ only by an accented capital do not match
in the SQL on either side (the safe direction for a cap would be to match). Fixing that properly means changing P1's SQL as well, so it belongs in one shared helper.
