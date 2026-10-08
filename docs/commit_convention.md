# Naming the work item in a commit

The briefs show which commits are tied to work and which are not ("Commits with no item reference"). A commit names a work item by writing its
**tracker id** in the message: `PM-` and **three or more digits**, exactly as the tracker numbers its items.

```
PM-031: show the unassigned list in the morning brief
```

- The id can be anywhere in the message (subject or body). The first one named is the item the commit is about.
- Use the id of a **tracker item** (`PM-031`, the blocked items approved from a channel). The plan's own rows (`PM-29`, two digits) are not tracker
  items, so a message that names only a plan row counts as **no item reference**. Write `PM-29:` freely in the subject; it is the plan row, and the
  brief will honestly call the commit unreferenced.
- A commit that is not about any tracked item (a dependency bump, a typo) simply names none. That is correct, not an error: the brief lists it so a
  person can see it, and does not accuse anyone.
- An id the tracker does not have (`PM-999`) is shown as "names PM-999, which is not in the tracker", never as linked work.

`.gitmessage` is a commit template that reminds you of this. To use it in a clone: `git config commit.template .gitmessage`.

## Where it shows up

- The sample-project morning brief: "Commits with no item reference" (computed from the seeded commit fixture).
- A real channel's brief, when repositories are configured for it with `PM_CHANNEL_REPOS` (see `docs/channel_brief.md`): the day's commits in two
  sections, "Commits with no item reference" and "Commits that name an item". Read-only (`git log`); nothing is fetched or written.
