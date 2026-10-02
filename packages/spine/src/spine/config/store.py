"""
The committed-config-plus-live-override pattern, generalized (CHN-33
extraction of P1's config/loader.py -- CHN-02/CHN-25's own design).

P1's own ChannelConfigStore is NOT rewritten to use this -- it keeps
its own concrete, already-live, already-tested channel_config/channels
tables exactly as they are (see DECISION_LOG.md; that schema is queried
directly by other parts of the running system, so migrating it is real,
separate surgery with no benefit tonight). What moves here is the
PATTERN itself, as genuinely new, generic, working code a future agent
can adopt for its own config need without copying P1's file and
find-and-replacing "roster" with something else.

The pattern, restated generically: committed YAML files are the system
of record (a fresh sync_to_db() always resets every field to whatever
is checked in); a narrow, explicitly-named subset of fields is
"owner-editable" -- changeable live, without a deploy, by whichever
surface a channel/project owner uses; every live edit re-validates the
WHOLE resulting config through the caller's own Pydantic model (an
invalid result is rejected outright, never partially written); and
every live edit appends exactly one audit row recording who changed
what and the before/after values, regardless of which surface made the
change.

STORAGE SHAPE, deliberately different from P1's own hand-mapped
per-field SQL columns: this generic version stores each config as one
JSON blob per id, in one generic table, rather than requiring a
bespoke migration per agent's own field set. That is what makes it
actually reusable across agents whose configs share almost none of the
same fields -- a per-column schema would have to be redesigned for
every consumer; a JSON blob does not. An agent that wants typed SQL
columns for its own config can still build that on top of (or instead
of) this store; this module optimizes for "works for any Pydantic
model with zero schema changes," not for queryability of individual
fields from plain SQL.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Generic, TypeVar

import yaml
from pydantic import BaseModel

from spine.storage.db import DEFAULT_DB_PATH, get_connection, run_migrations

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

T = TypeVar("T", bound=BaseModel)


class ConfigNotFoundError(KeyError):
    pass


class ConfigStore(Generic[T]):
    """A committed-YAML-plus-live-DB-override store for configs of
    Pydantic model type T.

    `id_field`: the model field that uniquely identifies a config
    (P1's ChannelConfig uses "channel_id"; another agent might use
    "project_id"). `editable_fields`: the exact, explicitly-named
    subset of T's fields a live edit is allowed to touch -- everything
    else stays committed-YAML-only, changed only by a redeploy
    (sync_to_db()). This mirrors CHN-25's own narrower-than-the-whole-
    model design choice, made explicit and enforced here rather than
    left to caller discipline.
    """

    def __init__(
        self,
        model: type[T],
        *,
        id_field: str,
        editable_fields: tuple[str, ...],
        config_dir: str | Path,
    ):
        self.model = model
        self.id_field = id_field
        self.editable_fields = editable_fields
        self.config_dir = Path(config_dir)

    def ensure_schema(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        """Applies this store's own two migrations (config_store,
        config_audit) if not already applied. Safe to call every time
        before using the store -- run_migrations() is itself idempotent."""
        run_migrations(db_path, migrations_dir=MIGRATIONS_DIR)

    # -- committed YAML, the system of record -----------------------

    def list_configured(self) -> list[T]:
        """Load every committed config file, in filename order. An
        invalid file raises -- it is never silently skipped or
        defaulted."""
        return [self._load_file(path) for path in sorted(self.config_dir.glob("*.yaml"))]

    def version(self) -> str:
        """A fingerprint of the whole store's current committed
        content, for cheap change-detection."""
        digest = hashlib.sha256()
        for path in sorted(self.config_dir.glob("*.yaml")):
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _load_file(self, path: Path) -> T:
        raw = yaml.safe_load(path.read_text())
        try:
            return self.model.model_validate(raw)
        except Exception as exc:
            raise ValueError(f"Invalid config in {path}: {exc}") from exc

    def sync_to_db(self, db_path: str | Path = DEFAULT_DB_PATH) -> int:
        """Upsert every committed config's FULL content into the live
        table -- the "redeploy" step. Resets every field, including the
        live-editable ones, back to whatever is checked in right now."""
        self.ensure_schema(db_path)
        configs = self.list_configured()
        conn = get_connection(db_path)
        try:
            for config in configs:
                cid = str(getattr(config, self.id_field))
                payload = config.model_dump(mode="json")
                conn.execute(
                    """
                    INSERT INTO spine_config_store (id, kind, payload, version, updated_at)
                    VALUES (:id, :kind, :payload, :version, :updated_at)
                    ON CONFLICT(id) DO UPDATE SET
                        payload = excluded.payload,
                        version = excluded.version,
                        updated_at = excluded.updated_at
                    """,
                    {
                        "id": cid,
                        "kind": self.model.__name__,
                        "payload": json.dumps(payload),
                        "version": payload.get("version", 1),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            conn.commit()
            return len(configs)
        finally:
            conn.close()

    # -- live DB, the effective/override view ------------------------

    def get_effective(self, config_id: str, *, db_path: str | Path = DEFAULT_DB_PATH) -> T:
        """The live read path: what a running job or an approval
        surface should treat as this config's CURRENT state, reflecting
        any live edit since the last sync_to_db(). Requires
        sync_to_db() to have run at least once for this id; a missing
        row raises rather than silently falling back to file content
        that may since have drifted from the DB."""
        self.ensure_schema(db_path)
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT payload FROM spine_config_store WHERE id = ?", (config_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ConfigNotFoundError(
                f"No config found for id={config_id!r} in {db_path!r} (has sync_to_db() run for it?)"
            )
        return self.model.model_validate(json.loads(row["payload"]))

    def update(
        self,
        config_id: str,
        changes: dict[str, object],
        *,
        updated_by: str,
        db_path: str | Path = DEFAULT_DB_PATH,
    ) -> T:
        """The one live write path. Only keys in self.editable_fields
        may be given -- anything else raises, rather than silently
        being ignored or silently succeeding. Reads the current
        effective config, applies the given changes, re-validates the
        WHOLE resulting config through self.model (an invalid result
        raises and nothing is written), then writes it back (bumping
        version) and appends exactly one audit row recording who
        changed what and the before/after values.

        Passing no changes is a no-op: returns the current config
        unchanged, writes nothing, appends no audit row -- matching
        P1's own update_channel_config() convention that an empty edit
        leaves no trace."""
        if not changes:
            return self.get_effective(config_id, db_path=db_path)

        illegal = set(changes) - set(self.editable_fields)
        if illegal:
            raise ValueError(
                f"Field(s) {sorted(illegal)} are not live-editable for {self.model.__name__} "
                f"(editable: {sorted(self.editable_fields)})"
            )

        current = self.get_effective(config_id, db_path=db_path)
        current_dump = current.model_dump(mode="json")
        diff: dict[str, dict[str, object]] = {}
        updated = current_dump.copy()
        for field, new_value in changes.items():
            diff[field] = {"old": current_dump.get(field), "new": new_value}
            updated[field] = new_value

        updated["version"] = current_dump.get("version", 1) + 1
        new_config = self.model.model_validate(updated)
        new_payload = new_config.model_dump(mode="json")

        conn = get_connection(db_path)
        try:
            conn.execute(
                """
                UPDATE spine_config_store SET payload = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(new_payload), new_payload["version"],
                 datetime.now(timezone.utc).isoformat(), config_id),
            )
            conn.execute(
                """
                INSERT INTO spine_config_audit (config_id, kind, actor, details, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (config_id, self.model.__name__, updated_by, json.dumps(diff),
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

        return new_config
