"""Tests for `tara doctor`'s PX4-backend checks (issue #177).

`_check_world_file`/`_check_px4_version`/`_check_gz_env` are exercised
directly — they're pure functions of a path, and testing them in isolation
avoids having to fake every other `run_checks` dependency (a real venv, npm,
node_modules) just to reach the PX4-specific ones. `_check_px4_version` is
tested against a real `git` checkout rather than a mocked `subprocess.run`:
the thing worth proving is that this project's exact `git describe` tag
matching behaves as expected against a real repo, including the untagged and
detached-HEAD cases a mock would have to assert into existence anyway.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tara_cli.config import TaraConfig
from tara_cli.doctor import (
    SUPPORTED_PX4_TAG,
    _check_gz_env,
    _check_node_version,
    _check_px4_version,
    _check_world_file,
    _node_version_satisfies_vite,
    run_checks,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def px4_git_repo(tmp_path: Path) -> Path:
    """A minimal real git checkout, so `git describe` behaves like it would
    against a real PX4-Autopilot clone."""
    repo = tmp_path / "PX4-Autopilot"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README").write_text("stand-in\n")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class TestWorldFileCheck:
    def test_missing_world_file_fails_with_issue_reference(self, tmp_path):
        result = _check_world_file(tmp_path)
        assert result.ok is False
        assert "sim/px4/worlds/drone_show_field.sdf" in result.detail.replace("\\", "/") or "drone_show_field.sdf" in result.detail
        assert "#177" in result.detail

    def test_present_world_file_passes(self, tmp_path):
        world = tmp_path / "sim" / "px4" / "worlds" / "drone_show_field.sdf"
        world.parent.mkdir(parents=True)
        world.write_text("<sdf></sdf>")

        result = _check_world_file(tmp_path)
        assert result.ok is True
        assert str(world) == result.detail


class TestPx4VersionCheck:
    def test_not_a_git_checkout_passes_but_cannot_verify(self, tmp_path):
        """No .git — e.g. a tarball extract. Not a failure, just unverifiable."""
        autopilot = tmp_path / "PX4-Autopilot"
        autopilot.mkdir()

        result = _check_px4_version(autopilot)
        assert result.ok is True
        assert "cannot verify" in result.detail
        assert SUPPORTED_PX4_TAG in result.detail

    def test_pinned_tag_passes(self, px4_git_repo):
        _git(px4_git_repo, "tag", SUPPORTED_PX4_TAG)

        result = _check_px4_version(px4_git_repo)
        assert result.ok is True
        assert result.detail == SUPPORTED_PX4_TAG

    def test_other_tag_fails_with_a_fix_command(self, px4_git_repo):
        _git(px4_git_repo, "tag", "v1.17.0")

        result = _check_px4_version(px4_git_repo)
        assert result.ok is False
        assert "v1.17.0" in result.detail
        assert f"checkout {SUPPORTED_PX4_TAG}" in result.detail

    def test_untagged_checkout_fails(self, px4_git_repo):
        """A plain clone of main, no tag at all."""
        result = _check_px4_version(px4_git_repo)
        assert result.ok is False
        assert "untagged or detached" in result.detail


class TestGzEnvCheck:
    def test_neither_location_fails_with_build_command(self, tmp_path):
        autopilot = tmp_path / "PX4-Autopilot"
        autopilot.mkdir()

        result = _check_gz_env(autopilot)
        assert result.ok is False
        assert "make px4_sitl gz_x500" in result.detail

    def test_old_layout_path_passes(self, tmp_path):
        autopilot = tmp_path / "PX4-Autopilot"
        old = autopilot / "build" / "px4_sitl_default" / "rootfs" / "gz_env.sh"
        old.parent.mkdir(parents=True)
        old.write_text("export GZ_SIM_RESOURCE_PATH=foo\n")

        result = _check_gz_env(autopilot)
        assert result.ok is True
        assert result.detail == str(old)

    def test_new_layout_path_passes(self, tmp_path):
        autopilot = tmp_path / "PX4-Autopilot"
        new = autopilot / "build" / "px4_sitl_default" / "gz_env.sh"
        new.parent.mkdir(parents=True)
        new.write_text("export GZ_SIM_RESOURCE_PATH=foo\n")

        result = _check_gz_env(autopilot)
        assert result.ok is True
        assert result.detail == str(new)

    def test_old_layout_preferred_when_both_exist(self, tmp_path):
        autopilot = tmp_path / "PX4-Autopilot"
        old = autopilot / "build" / "px4_sitl_default" / "rootfs" / "gz_env.sh"
        new = autopilot / "build" / "px4_sitl_default" / "gz_env.sh"
        old.parent.mkdir(parents=True)
        new.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("old\n")
        new.write_text("new\n")

        result = _check_gz_env(autopilot)
        assert result.detail == str(old)


class TestNodeVersionCheck:
    """`_node_version_satisfies_vite` is tested as a pure function over
    (major, minor) pairs — real Node installs spanning 18.x through 23.x
    aren't available to exercise in one test run, and the thing worth
    proving is the exact boundary logic (the 21.x line fails; 20.19 is the
    first 20.x that passes; 22.12 is the first 22.x that passes), not
    whatever Node happens to be on the machine running this test. The
    real-subprocess path (`_check_node_version` shelling out to `node
    --version`) is covered once by `TestRunChecksIntegration`, matching this
    file's own convention of testing `_check_px4_version` against a real git
    checkout rather than a mock.
    """

    @pytest.mark.parametrize(
        "major,minor",
        [(20, 19), (20, 20), (20, 99), (22, 12), (22, 50), (23, 0), (30, 0)],
    )
    def test_satisfying_versions(self, major, minor):
        assert _node_version_satisfies_vite(major, minor) is True

    @pytest.mark.parametrize(
        "major,minor",
        [(18, 19), (20, 18), (20, 0), (21, 0), (21, 99), (22, 0), (22, 11)],
    )
    def test_non_satisfying_versions(self, major, minor):
        assert _node_version_satisfies_vite(major, minor) is False

    def test_real_node_version_check_runs(self):
        """Exercises the real subprocess call; doesn't assert pass/fail since
        that depends on whatever Node this test machine has installed."""
        result = _check_node_version()
        assert result.name == "Node.js version"
        assert isinstance(result.ok, bool)


class TestRunChecksIntegration:
    def test_sim_backend_skips_all_px4_checks(self, tmp_path):
        config = TaraConfig(repo_root=tmp_path, backend="sim", px4_autopilot_dir=str(tmp_path / "PX4-Autopilot"))
        names = {r.name for r in run_checks(config)}
        assert "drone_show_field.sdf world" not in names
        assert "PX4 version" not in names
        assert "PX4 gz_env.sh" not in names

    def test_px4_backend_includes_the_new_checks(self, tmp_path):
        autopilot = tmp_path / "PX4-Autopilot"
        autopilot.mkdir()
        config = TaraConfig(repo_root=tmp_path, backend="px4", px4_autopilot_dir=str(autopilot))

        results = {r.name: r for r in run_checks(config)}
        assert "drone_show_field.sdf world" in results
        assert results["drone_show_field.sdf world"].ok is False
        assert "PX4 version" in results
        assert "PX4 gz_env.sh" in results
        assert results["PX4 gz_env.sh"].ok is False

    def test_px4_version_and_gz_env_skipped_when_autopilot_dir_absent(self, tmp_path):
        """No PX4-Autopilot checkout at all yet — don't pile on confusing
        secondary failures beyond the existing 'PX4-Autopilot build' check."""
        config = TaraConfig(repo_root=tmp_path, backend="px4", px4_autopilot_dir=str(tmp_path / "nonexistent"))
        names = {r.name for r in run_checks(config)}
        assert "PX4 version" not in names
        assert "PX4 gz_env.sh" not in names
        assert "drone_show_field.sdf world" in names  # repo-local, always checkable
