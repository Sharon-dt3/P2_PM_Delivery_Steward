"""
Real, independent proof of spine.config.store.ConfigStore against a
throwaway Pydantic model that has NOTHING to do with P1's ChannelConfig
-- this is the whole point of CHN-33's acceptance test: the pattern
must work for a config it has never seen before, not just replay P1's
own fields back at itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel
from spine.config.store import ConfigNotFoundError, ConfigStore
from spine.storage.db import (
    DEFAULT_DB_PATH,  # noqa: F401 -- documents the default exists
)


class ProjectConfig(BaseModel):
    """A deliberately different shape from ChannelConfig: no roster, no
    timezone, no Teams-flavored anything -- a totally different agent's
    config, to prove the pattern generalizes rather than just working
    for the one shape it was designed against."""

    project_id: str
    owner_email: str
    priority_threshold: int
    tags: list[str] = []
    version: int = 1


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore[ProjectConfig]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "proj-alpha.yaml").write_text(
        yaml.dump({
            "project_id": "proj-alpha",
            "owner_email": "owner@example.com",
            "priority_threshold": 3,
            "tags": ["demo"],
        })
    )
    return ConfigStore(
        ProjectConfig,
        id_field="project_id",
        editable_fields=("owner_email", "priority_threshold"),
        config_dir=config_dir,
    )


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


def test_list_configured_loads_committed_yaml(store: ConfigStore[ProjectConfig]):
    configs = store.list_configured()
    assert len(configs) == 1
    assert configs[0].project_id == "proj-alpha"
    assert configs[0].priority_threshold == 3


def test_get_effective_raises_before_sync(store: ConfigStore[ProjectConfig], db_path: str):
    with pytest.raises(ConfigNotFoundError):
        store.get_effective("proj-alpha", db_path=db_path)


def test_sync_then_get_effective_matches_committed(store: ConfigStore[ProjectConfig], db_path: str):
    n = store.sync_to_db(db_path=db_path)
    assert n == 1
    live = store.get_effective("proj-alpha", db_path=db_path)
    assert live.owner_email == "owner@example.com"
    assert live.priority_threshold == 3
    assert live.version == 1


def test_update_only_touches_editable_fields_and_bumps_version(
    store: ConfigStore[ProjectConfig], db_path: str
):
    store.sync_to_db(db_path=db_path)
    updated = store.update(
        "proj-alpha", {"priority_threshold": 9}, updated_by="test-suite", db_path=db_path,
    )
    assert updated.priority_threshold == 9
    assert updated.version == 2
    assert updated.owner_email == "owner@example.com"  # untouched field survives

    live = store.get_effective("proj-alpha", db_path=db_path)
    assert live.priority_threshold == 9
    assert live.version == 2


def test_update_rejects_non_editable_field(store: ConfigStore[ProjectConfig], db_path: str):
    store.sync_to_db(db_path=db_path)
    with pytest.raises(ValueError, match="not live-editable"):
        store.update("proj-alpha", {"tags": ["not", "allowed"]}, updated_by="test-suite", db_path=db_path)


def test_update_with_no_changes_is_a_true_no_op(store: ConfigStore[ProjectConfig], db_path: str):
    store.sync_to_db(db_path=db_path)
    before = store.get_effective("proj-alpha", db_path=db_path)
    result = store.update("proj-alpha", {}, updated_by="test-suite", db_path=db_path)
    assert result.version == before.version  # no version bump for an empty edit


def test_update_writes_exactly_one_audit_row_with_before_and_after(
    store: ConfigStore[ProjectConfig], db_path: str
):
    from spine.storage.db import get_connection

    store.sync_to_db(db_path=db_path)
    store.update("proj-alpha", {"priority_threshold": 7}, updated_by="alice", db_path=db_path)

    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT actor, details FROM spine_config_audit WHERE config_id = ?", ("proj-alpha",),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["actor"] == "alice"
    import json
    details = json.loads(rows[0]["details"])
    assert details["priority_threshold"] == {"old": 3, "new": 7}


def test_sync_to_db_resets_a_live_edit_back_to_committed(store: ConfigStore[ProjectConfig], db_path: str):
    store.sync_to_db(db_path=db_path)
    store.update("proj-alpha", {"priority_threshold": 99}, updated_by="test-suite", db_path=db_path)

    # a fresh "redeploy" -- resets to whatever is checked in, per the module's own docstring
    store.sync_to_db(db_path=db_path)
    live = store.get_effective("proj-alpha", db_path=db_path)
    assert live.priority_threshold == 3  # back to committed, not the live edit
    assert live.version == 1


def test_invalid_committed_yaml_raises_not_silently_skipped(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "broken.yaml").write_text(yaml.dump({"project_id": "broken"}))  # missing required fields

    store = ConfigStore(
        ProjectConfig, id_field="project_id", editable_fields=(), config_dir=config_dir,
    )
    with pytest.raises(ValueError, match="Invalid config"):
        store.list_configured()
