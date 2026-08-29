"""Tests for scripts/update_ash.py, the pinned-version checker/updater for
scripts/ash (the Automated Security Helper wrapper security_review's
mandatory scan depends on). Network calls are mocked -- these must not
depend on GitHub being reachable."""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "update_ash.py"
_spec = importlib.util.spec_from_file_location("update_ash", _MODULE_PATH)
update_ash = importlib.util.module_from_spec(_spec)
sys.modules["update_ash"] = update_ash
_spec.loader.exec_module(update_ash)


ASH_TEMPLATE = '#!/bin/sh\nexec uvx "git+https://github.com/awslabs/automated-security-helper.git@{tag}" "$@"\n'


@pytest.fixture
def ash_script(tmp_path):
    path = tmp_path / "ash"
    path.write_text(ASH_TEMPLATE.format(tag="v3.5.4"))
    return path


def _published(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestCurrentPin:
    def test_extracts_the_pinned_tag(self, ash_script):
        assert update_ash.current_pin(ash_script) == "v3.5.4"

    def test_raises_when_no_pin_found(self, tmp_path):
        path = tmp_path / "ash"
        path.write_text("#!/bin/sh\necho no pin here\n")
        with pytest.raises(RuntimeError):
            update_ash.current_pin(path)


class TestApplyUpdate:
    def test_rewrites_only_the_version_tag(self, ash_script):
        update_ash.apply_update(ash_script, "v3.7.0")
        text = ash_script.read_text()
        assert "v3.7.0" in text
        assert "v3.5.4" not in text
        assert 'exec uvx "git+https://github.com/awslabs/automated-security-helper.git@v3.7.0" "$@"' in text

    def test_raises_when_pin_pattern_not_found(self, tmp_path):
        path = tmp_path / "ash"
        path.write_text("#!/bin/sh\necho no pin here\n")
        with pytest.raises(RuntimeError):
            update_ash.apply_update(path, "v3.7.0")


class TestAgeDays:
    def test_computes_days_since_publish(self):
        assert update_ash.age_days(_published(78)) == 78


class TestMainCheckMode:
    def test_check_reports_fresh_and_exits_zero(self, ash_script, capsys):
        with patch.object(update_ash, "release_info", return_value={"published_at": _published(10)}), \
             patch.object(update_ash, "latest_release", return_value={"tag_name": "v3.5.4"}):
            code = update_ash.main(["--check", "--ash-script", str(ash_script)])

        assert code == 0
        assert "v3.5.4" in ash_script.read_text()  # untouched
        assert "Within the freshness threshold" in capsys.readouterr().out

    def test_check_reports_stale_and_exits_one(self, ash_script, capsys):
        with patch.object(update_ash, "release_info", return_value={"published_at": _published(120)}), \
             patch.object(update_ash, "latest_release", return_value={"tag_name": "v3.7.0"}):
            code = update_ash.main(["--check", "--ash-script", str(ash_script)])

        assert code == 1
        assert "v3.5.4" in ash_script.read_text()  # --check must not modify anything
        assert "STALE" in capsys.readouterr().out

    def test_stale_days_threshold_is_configurable(self, ash_script):
        with patch.object(update_ash, "release_info", return_value={"published_at": _published(10)}), \
             patch.object(update_ash, "latest_release", return_value={"tag_name": "v3.5.4"}):
            code = update_ash.main(["--check", "--stale-days", "5", "--ash-script", str(ash_script)])

        assert code == 1

    def test_network_failure_returns_two_and_leaves_file_untouched(self, ash_script):
        with patch.object(update_ash, "release_info", side_effect=OSError("network down")):
            code = update_ash.main(["--check", "--ash-script", str(ash_script)])

        assert code == 2
        assert "v3.5.4" in ash_script.read_text()


class TestMainApplyMode:
    def test_updates_the_pin_when_behind(self, ash_script):
        with patch.object(update_ash, "release_info", return_value={"published_at": _published(120)}), \
             patch.object(update_ash, "latest_release", return_value={"tag_name": "v3.7.0"}):
            code = update_ash.main(["--ash-script", str(ash_script)])

        assert code == 0
        assert "v3.7.0" in ash_script.read_text()

    def test_no_op_when_already_latest(self, ash_script, capsys):
        with patch.object(update_ash, "release_info", return_value={"published_at": _published(120)}), \
             patch.object(update_ash, "latest_release", return_value={"tag_name": "v3.5.4"}):
            code = update_ash.main(["--ash-script", str(ash_script)])

        assert code == 0
        assert "v3.5.4" in ash_script.read_text()
        assert "nothing to update" in capsys.readouterr().out.lower()
