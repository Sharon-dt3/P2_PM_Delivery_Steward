"""PM-19: the promotion threshold is configuration, never a literal in the code.

It comes from a committed file (config/risk_promotion.yaml) and can be overridden by an
environment variable. A missing file means "no age requirement" (every gap is proposed,
as in PM-16); a file or variable that is present but wrong is an error, never silently
replaced by a default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pm.risk.promotion_config import PromotionConfigError, load_promotion_policy

REPO = Path(__file__).resolve().parents[2]


def _file(tmp_path, text):
    path = tmp_path / "risk_promotion.yaml"
    path.write_text(text)
    return path


def test_the_threshold_is_read_from_the_file(tmp_path):
    assert load_promotion_policy(path=_file(tmp_path, "threshold_days: 2\n"), env={}).threshold_days == 2


@pytest.mark.parametrize("value", [0, 1, 3, 7, 30])
def test_any_value_in_the_file_is_the_value_used(tmp_path, value):
    assert load_promotion_policy(path=_file(tmp_path, f"threshold_days: {value}\n"), env={}).threshold_days == value


def test_the_environment_overrides_the_file(tmp_path):
    policy = load_promotion_policy(path=_file(tmp_path, "threshold_days: 2\n"), env={"PM_RISK_PROMOTION_THRESHOLD_DAYS": "5"})

    assert policy.threshold_days == 5 and "environment" in policy.source


def test_an_empty_environment_value_is_ignored(tmp_path):
    policy = load_promotion_policy(path=_file(tmp_path, "threshold_days: 2\n"), env={"PM_RISK_PROMOTION_THRESHOLD_DAYS": "  "})

    assert policy.threshold_days == 2


def test_a_missing_file_means_no_age_requirement(tmp_path):
    assert load_promotion_policy(path=tmp_path / "nope.yaml", env={}) is None


def test_an_explicit_null_means_no_age_requirement(tmp_path):
    assert load_promotion_policy(path=_file(tmp_path, "threshold_days: null\n"), env={}) is None
    assert load_promotion_policy(path=_file(tmp_path, "# nothing set\n"), env={}) is None


@pytest.mark.parametrize("bad", ["-1", "2.5", "two", "true", "[1]", "'3 days'"])
def test_a_wrong_value_in_the_file_is_an_error_not_a_default(tmp_path, bad):
    with pytest.raises(PromotionConfigError):
        load_promotion_policy(path=_file(tmp_path, f"threshold_days: {bad}\n"), env={})


@pytest.mark.parametrize("bad", ["-1", "2.5", "two", "1e3", "0x2"])
def test_a_wrong_environment_value_is_an_error(tmp_path, bad):
    with pytest.raises(PromotionConfigError):
        load_promotion_policy(path=_file(tmp_path, "threshold_days: 2\n"), env={"PM_RISK_PROMOTION_THRESHOLD_DAYS": bad})


def test_a_file_that_is_not_valid_yaml_is_an_error(tmp_path):
    with pytest.raises(PromotionConfigError):
        load_promotion_policy(path=_file(tmp_path, "threshold_days: [unclosed\n"), env={})


def test_a_file_that_is_not_a_mapping_is_an_error(tmp_path):
    with pytest.raises(PromotionConfigError):
        load_promotion_policy(path=_file(tmp_path, "- 2\n"), env={})


def test_the_path_can_come_from_the_environment(tmp_path):
    path = _file(tmp_path, "threshold_days: 9\n")

    assert load_promotion_policy(env={"PM_RISK_PROMOTION_CONFIG": str(path)}).threshold_days == 9


def test_the_default_file_does_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    policy = load_promotion_policy(env={})

    assert policy is not None and isinstance(policy.threshold_days, int) and policy.threshold_days >= 0


def test_the_committed_file_exists_and_names_the_setting():
    text = (REPO / "config" / "risk_promotion.yaml").read_text()

    assert "threshold_days" in text and "older than" in text  # documented where it is set


def test_the_source_says_where_the_value_came_from(tmp_path):
    path = _file(tmp_path, "threshold_days: 2\n")

    assert str(path) in load_promotion_policy(path=path, env={}).source


def test_no_threshold_number_is_written_into_the_promotion_code():
    """The value is configuration: the modules that apply it contain no literal day count to compare against."""
    import re

    for name in ("promotion.py", "age.py"):
        source = (REPO / "src" / "pm" / "risk" / name).read_text()
        code = re.sub(r'""".*?"""|#.*', "", source, flags=re.DOTALL)
        assert not re.search(r"(>|>=|<|<=|==)\s*\d+\b(?!\.)", code.replace("> 0", "").replace(">= 0", "").replace("== 0", "")), name
