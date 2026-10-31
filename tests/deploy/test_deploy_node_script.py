"""scripts/deploy-node.sh: the drain rule, the deploy receipt and the rollback.

``scripts/deploy-node.sh`` installs a harness node straight from a git ref
into the node's existing venv.  Three things about it are load-bearing and
are pinned here:

* the DRAIN RULE -- a deploy restarts ``lh-harness.service`` and kills every
  in-flight run, so the script refuses while a run is alive (it reads
  ``runs/<id>/control/status.json``) unless ``--drain-wait N`` is given;
* the RECEIPT -- ``<state>/deployed.json`` records
  ``{commit, version, at, by, previous}``, so the deployed commit is readable
  over ``/api/meta`` without a shell;
* the ROLLBACK -- a failed read-back restores the previous bytes from the
  backups taken before the install, so the node is left as it was.

These tests drive the REAL script against a throwaway venv and a LOCAL git
repository (no network, no host), with ``systemctl`` and ``curl`` shimmed on
PATH.  Nothing here touches a real node.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "deploy-node.sh"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None or shutil.which("git") is None,
    reason="deploy-node.sh needs bash and git",
)


def _make_venv(path: Path) -> None:
    try:
        venv.create(path, with_pip=True)
        return
    except (Exception, SystemExit):  # venv exits 1 when ensurepip is missing
        shutil.rmtree(path, ignore_errors=True)
    probe = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("neither ensurepip nor a runner pip is available to build a test venv")
    venv.create(path, with_pip=False)
    pip = path / "bin" / "pip"
    pip.write_text(
        f'#!/usr/bin/env bash\nexec "{sys.executable}" -m pip --python "{path}/bin/python" "$@"\n',
        encoding="utf-8",
    )
    pip.chmod(0o755)


# Building a venv with ensurepip costs ~2 minutes, and the suite needs one
# per test (each test installs into it).  The venv is therefore built ONCE
# per session into a template and COPIED per test: a copy takes milliseconds.
# The copy is what the node under test installs into, so it is still a real
# venv with a real pip.
_VENV_TEMPLATE: Path | None = None


@pytest.fixture(scope="session")
def venv_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    global _VENV_TEMPLATE
    if _VENV_TEMPLATE is not None:
        return _VENV_TEMPLATE
    template = tmp_path_factory.mktemp("venv-template") / "venv"
    _make_venv(template)
    _VENV_TEMPLATE = template
    return template


def _clone_venv(template: Path, target: Path) -> None:
    """Copy the prepared venv to the node's path.

    A venv is not relocatable by plain copy (the scripts in bin/ have the
    build path baked in), so the copy is followed by a rewrite of that path.
    The rewrite is what keeps the copy a working venv.
    """
    shutil.copytree(template, target, symlinks=True)
    bin_dir = target / "bin"
    for script in list(bin_dir.iterdir()):
        if not script.is_file():
            continue
        try:
            text = script.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if str(template) not in text:
            continue
        script.write_text(text.replace(str(template), str(target)), encoding="utf-8")
    pyvenv = target / "pyvenv.cfg"
    if pyvenv.is_file():
        pyvenv.write_text(
            pyvenv.read_text(encoding="utf-8").replace(str(template), str(target)),
            encoding="utf-8",
        )


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "deploy-test",
        "GIT_AUTHOR_EMAIL": "deploy-test@example.invalid",
        "GIT_COMMITTER_NAME": "deploy-test",
        "GIT_COMMITTER_EMAIL": "deploy-test@example.invalid",
    }
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


class Node:
    """A fake harness node: venv, runs root, local git repo, shims."""

    def __init__(self, root: Path, venv_template: Path) -> None:
        self.root = root
        self.venv = root / "venv"
        self.home = root / "home"
        self.state = self.home / ".lh-harness"
        self.runs = root / "runs"
        self.repo = root / "source-repo"
        self.repo_dir = root / "deploy-repo"
        self.backups = self.state / "backups"
        self.receipt = self.state / "deployed.json"
        self.settings_db = self.state / "settings.db"
        for directory in (self.state, self.runs, self.repo):
            directory.mkdir(parents=True, exist_ok=True)
        self.settings_db.write_text('{"queue": {"trios": []}}\n', encoding="utf-8")

        # A local git repository that stands in for the upstream repo: the
        # script clones it and installs from it.  No network is involved.
        _git(self.repo, "init", "--quiet", "--initial-branch=main")
        self.write_source("old")
        self.old_sha = self.commit("old")
        self.write_source("new")
        self.new_sha = self.commit("new")

        self.shims = root / "shims"
        self.shims.mkdir()
        self.service_state = root / "service-state"
        self.service_state.write_text("active\n", encoding="utf-8")
        self.health_code = "200"
        self.meta_commit = ""            # what /api/meta reports
        self.meta_version = "0.1.7"
        self.restart_count = 0
        self._write_shims()
        self.env = {
            **os.environ,
            "PATH": f"{self.shims}{os.pathsep}{os.environ.get('PATH', '')}",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "LH_VENV": str(self.venv),
            "LH_SERVICE": "lh-harness.service",
            "LH_HARNESS_HOME": str(self.home),
            "LH_DEPLOY_STATE": str(self.state),
            "LH_SETTINGS_DB": str(self.settings_db),
            "LH_RECEIPT": str(self.receipt),
            "LH_BACKUP_DIR": str(self.backups),
            "LH_RUNS_ROOT": str(self.runs),
            "LH_API_URL": "http://127.0.0.1:8799",
            "LH_REPO_URL": str(self.repo),
            "LH_REPO_DIR": str(self.repo_dir),
            "LH_DEPLOYED_BY": "deploy-test",
            "HOME": str(self.home),
        }
        self.env.pop("PYTHONPATH", None)
        _clone_venv(venv_template, self.venv)

    # --- the fake upstream repository -------------------------------------

    def write_source(self, marker: str) -> None:
        """Write a minimal installable lh_harness package into the repo."""
        package = self.repo / "src" / "lh_harness"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text(f'__version__ = "0.1.7"\n# {marker}\n', encoding="utf-8")
        # The fixture mirrors the REAL src/lh_harness/build_info.py: the
        # package's own commit comes from a stamped _build_info.json, and
        # build_info() PREFERS the deploy receipt over it (that is what makes
        # /api/meta report the deployed commit).  The deploy script must
        # therefore read package_build_info() -- the package-only view -- when
        # it verifies what it just installed, or the second deploy on a node
        # would verify against the PREVIOUS deploy's receipt.
        (package / "build_info.py").write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "from . import __version__\n"
            "def _read_commit():\n"
            "    p = Path(__file__).with_name('_build_info.json')\n"
            "    try:\n"
            "        return json.loads(p.read_text())['commit']\n"
            "    except Exception:\n"
            "        return None\n"
            "def _read_deployed_commit():\n"
            "    # Mirrors the REAL build_info._deployed_receipt_paths(): the\n"
            "    # override, then $LH_HARNESS_HOME/.lh-harness/deployed.json,\n"
            "    # then ~/.lh-harness/deployed.json.  The deploy script sets\n"
            "    # LH_HARNESS_HOME to the node's home, so on a real node this\n"
            "    # DOES find the receipt -- which is exactly why the deploy\n"
            "    # script must read package_build_info() (the package-only\n"
            "    # view) when it verifies what it just installed.\n"
            "    override = os.environ.get('LH_HARNESS_DEPLOYED_JSON', '').strip()\n"
            "    roots = []\n"
            "    if override:\n"
            "        roots.append(Path(override))\n"
            "    else:\n"
            "        home = os.environ.get('LH_HARNESS_HOME', '').strip()\n"
            "        if home:\n"
            "            roots.append(Path(home) / '.lh-harness' / 'deployed.json')\n"
            "        roots.append(Path.home() / '.lh-harness' / 'deployed.json')\n"
            "    for p in roots:\n"
            "        try:\n"
            "            d = json.loads(p.read_text())\n"
            "        except Exception:\n"
            "            continue\n"
            "        if isinstance(d, dict):\n"
            "            c = (d.get('commit') or '').strip()\n"
            "            if c:\n"
            "                return c\n"
            "    return None\n"
            "def package_build_info():\n"
            "    # The PACKAGE-only view: the real module exposes this so the\n"
            "    # deploy script can read the package's own commit without the\n"
            "    # deploy receipt overriding it.\n"
            "    return {'version': __version__, 'commit': _read_commit()}\n"
            "def build_info():\n"
            "    info = dict(package_build_info())\n"
            "    deployed = _read_deployed_commit()\n"
            "    if deployed:\n"
            "        info['commit'] = deployed\n"
            "    return info\n"
            "if __name__ == '__main__':\n"
            "    print(build_info()['commit'] or '')\n",
            encoding="utf-8",
        )
        (self.repo / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
            '[project]\nname = "lh-harness"\nversion = "0.1.7"\n'
            'description = "test fixture"\nrequires-python = ">=3.9"\n\n'
            '[tool.hatch.build.targets.wheel]\npackages = ["src/lh_harness"]\n',
            encoding="utf-8",
        )

    def commit(self, marker: str) -> str:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "-m", marker)
        return _git(self.repo, "rev-parse", "HEAD")

    # --- the shims ---------------------------------------------------------

    def _write_shims(self) -> None:
        systemctl = self.shims / "systemctl"
        systemctl.write_text(
            "#!/usr/bin/env bash\n"
            'state_file="$LH_TEST_STATE/service-state"\n'
            'case "$1" in\n'
            '  is-active) cat "$state_file"; exit 0 ;;\n'
            '  restart) n=$(cat "$LH_TEST_STATE/restart-count" 2>/dev/null || echo 0);'
            ' echo $((n+1)) > "$LH_TEST_STATE/restart-count"; exit 0 ;;\n'
            'esac\n'
            "exit 0\n",
            encoding="utf-8",
        )
        curl = self.shims / "curl"
        curl.write_text(
            "#!/usr/bin/env bash\n"
            'url="${@: -1}"\n'
            'case "$url" in\n'
            '  */api/health) code=$(cat "$LH_TEST_STATE/health-code");'
            ' printf "%s" "$code"; exit $([ "$code" = 200 ] && echo 0 || echo 22) ;;\n'
            '  */api/meta) printf \'{"build": {"version": "%s", "commit": "%s"}}\\n\''
            ' "$(cat "$LH_TEST_STATE/meta-version")" "$(cat "$LH_TEST_STATE/meta-commit")"; exit 0 ;;\n'
            'esac\n'
            "exit 7\n",
            encoding="utf-8",
        )
        for shim in (systemctl, curl):
            shim.chmod(0o755)

    # --- the run state -----------------------------------------------------

    def write_status(self, run_id: str, status: str) -> None:
        control = self.runs / run_id / "control"
        control.mkdir(parents=True, exist_ok=True)
        (control / "status.json").write_text(json.dumps({"status": status, "alive": True}), encoding="utf-8")

    def install_previous(self) -> None:
        """Install the OLD source into the venv, as a live node would have."""
        subprocess.run(
            [str(self.venv / "bin" / "pip"), "install", "--quiet", "--no-deps", str(self.repo)],
            env=self.env, check=True, capture_output=True, text=True,
        )

    # --- running the script ------------------------------------------------

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            **self.env,
            "LH_TEST_STATE": str(self.root),
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=600,
        )

    def receipt_data(self) -> dict:
        return json.loads(self.receipt.read_text(encoding="utf-8"))

    def installed_marker(self) -> str:
        result = subprocess.run(
            [str(self.venv / "bin" / "python"), "-c",
             "import lh_harness, pathlib; print(pathlib.Path(lh_harness.__file__).read_text())"],
            env=self.env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout


@pytest.fixture()
def node(tmp_path: Path, venv_template: Path) -> Node:
    node = Node(tmp_path, venv_template)
    node.install_previous()
    # The API reports the commit of whatever the node runs.  A live node
    # reports the commit its package was built from; the shim starts with the
    # old one and the test moves it.
    (node.root / "meta-commit").write_text(node.old_sha, encoding="utf-8")
    (node.root / "meta-version").write_text("0.1.7", encoding="utf-8")
    (node.root / "health-code").write_text("200", encoding="utf-8")
    (node.root / "restart-count").write_text("0", encoding="utf-8")
    return node


# --- the drain rule ---------------------------------------------------------


@pytest.mark.parametrize("status", ["creating", "starting", "running", "waiting_approval", "stopping"])
def test_refuses_while_a_run_is_alive(node: Node, status: str) -> None:
    node.write_status("run-alive", status)

    result = node.run("--ref", "main")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "live run(s)" in result.stderr
    # Nothing was installed, restarted or written.
    assert (node.root / "restart-count").read_text().strip() == "0"
    assert not node.receipt.exists()


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "blocked", "incomplete"])
def test_a_finished_run_does_not_block_the_deploy(node: Node, status: str) -> None:
    node.write_status("run-done", status)
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (node.root / "restart-count").read_text().strip() == "1"


def test_a_run_without_a_status_file_does_not_block_the_deploy(node: Node) -> None:
    # The supervisor writes status.json atomically; a missing file means the
    # worker never opened the run directory -- not an alive run.
    (node.runs / "run-unstarted").mkdir(parents=True, exist_ok=True)
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr


def test_drain_wait_lets_the_deploy_proceed_when_the_run_finishes(node: Node) -> None:
    node.write_status("run-alive", "running")
    # The run finishes shortly after the deploy starts waiting.
    finished = node.root / "run-finished"

    def _finish() -> None:
        # Flip the status to a terminal one after a short delay.
        import threading
        import time

        def flip() -> None:
            time.sleep(8)
            node.write_status("run-alive", "completed")
            finished.write_text("done", encoding="utf-8")

        threading.Thread(target=flip, daemon=True).start()

    _finish()
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main", "--drain-wait", "120")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "drain check" in result.stdout


def test_drain_wait_that_expires_aborts(node: Node) -> None:
    node.write_status("run-alive", "running")

    result = node.run("--ref", "main", "--drain-wait", "1")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "still alive" in result.stderr
    assert (node.root / "restart-count").read_text().strip() == "0"


# --- the receipt ------------------------------------------------------------


def test_the_receipt_records_the_deployed_commit(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    data = node.receipt_data()
    assert data["commit"] == node.new_sha
    assert data["version"] == "0.1.7"
    assert data["by"] == "deploy-test"
    assert data["at"].endswith("Z")
    assert data["previous"] is None


def test_the_receipt_records_the_previous_deploy(node: Node) -> None:
    node.receipt.write_text(
        json.dumps({"commit": node.old_sha, "version": "0.1.7", "at": "2026-10-01T00:00:00Z",
                    "by": "someone-else", "previous": None}),
        encoding="utf-8",
    )
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    data = node.receipt_data()
    assert data["commit"] == node.new_sha
    assert data["previous"] == {
        "commit": node.old_sha,
        "version": "0.1.7",
        "at": "2026-10-01T00:00:00Z",
        "by": "someone-else",
    }


def test_a_second_deploy_verifies_against_the_package_not_the_old_receipt(
    node: Node,
) -> None:
    """The install verify must read the PACKAGE's commit, not the receipt's.

    build_info() prefers the deploy receipt (that is what makes /api/meta
    report the deployed commit), so if the deploy script verified what it had
    just installed through build_info(), the SECOND deploy on a node would
    read the PREVIOUS deploy's receipt and fail its own verify against a
    commit it never installed.  The script therefore reads
    package_build_info() -- the package-only view.
    """
    # A previous deploy left its receipt behind.
    node.receipt.write_text(
        json.dumps({"commit": node.old_sha, "version": "0.1.7", "at": "2026-10-01T00:00:00Z",
                    "by": "someone-else", "previous": None}),
        encoding="utf-8",
    )
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    # The install verify compared against the commit that was installed.
    assert f"expected '{node.new_sha}'" not in result.stderr, result.stderr
    # And the receipt now names the NEW commit.
    assert node.receipt_data()["commit"] == node.new_sha


def test_the_one_line_receipt_is_printed(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    line = [row for row in result.stdout.splitlines() if row.startswith("DEPLOY_OK ")]
    assert len(line) == 1, result.stdout
    fields = dict(field.split("=", 1) for field in line[0].split()[1:])
    assert fields["commit"] == node.new_sha
    assert fields["version"] == "0.1.7"
    assert fields["by"] == "deploy-test"
    assert fields["previous"] == "none"


def test_the_backups_carry_a_utc_suffix(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    names = sorted(path.name for path in node.backups.iterdir())
    assert any(name.startswith("settings.db.") for name in names), names
    assert any(name.startswith("package-metadata.") for name in names), names
    stamps = {name.split(".")[-1] for name in names if name.count(".") >= 2}
    for stamp in stamps:
        # A UTC stamp: 20261026T101112Z.
        assert len(stamp) == 16 and stamp.endswith("Z"), stamp
        assert stamp[8] == "T" and stamp[15] == "Z", stamp
    # The settings backup is a COPY of the live file, not a move.
    backups = [path for path in node.backups.iterdir() if path.name.startswith("settings.db.")]
    assert backups and backups[0].read_text() == node.settings_db.read_text()
    assert node.settings_db.exists()


# --- the rollback -----------------------------------------------------------


def test_a_failed_read_back_rolls_back_to_the_previous_code(node: Node) -> None:
    # The service comes back but /api/meta reports a DIFFERENT commit: the
    # read-back fails and the node must go back to the pre-deploy bytes.
    (node.root / "meta-commit").write_text("f" * 40, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 4, result.stdout + result.stderr
    assert "READ-BACK FAILED" in result.stderr
    assert "ROLLBACK" in result.stderr
    # The previous code is back.
    assert "# old" in node.installed_marker()
    # And the receipt does not name a commit the node does not run.
    assert not node.receipt.exists()


def test_a_failed_health_read_back_rolls_back(node: Node) -> None:
    (node.root / "health-code").write_text("503", encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 4, result.stdout + result.stderr
    assert "/api/health" in result.stderr
    assert "# old" in node.installed_marker()


def test_a_failed_read_back_restores_the_previous_receipt(node: Node) -> None:
    node.receipt.write_text(
        json.dumps({"commit": node.old_sha, "version": "0.1.7", "at": "2026-10-01T00:00:00Z",
                    "by": "someone-else", "previous": None}),
        encoding="utf-8",
    )
    (node.root / "meta-commit").write_text("f" * 40, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 4, result.stdout + result.stderr
    # The previous receipt is back: it names the commit the node runs again.
    assert node.receipt_data()["commit"] == node.old_sha


def test_the_rollback_restores_the_settings_store(node: Node) -> None:
    (node.root / "meta-commit").write_text("f" * 40, encoding="utf-8")
    before = node.settings_db.read_text()

    result = node.run("--ref", "main")

    assert result.returncode == 4, result.stdout + result.stderr
    assert node.settings_db.read_text() == before


# --- the general shape ------------------------------------------------------


def test_the_script_never_opens_the_secrets_file(node: Node) -> None:
    # The node's credentials live in the unit's EnvironmentFile.  The deploy
    # must not read it: the read-back authenticates with the token from the
    # environment only.
    secrets = node.home / ".lh-harness-secrets.env"
    secrets.write_text("LH_HARNESS_WEB_TOKEN=super-secret-value\n", encoding="utf-8")
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "super-secret-value" not in result.stdout
    assert "super-secret-value" not in result.stderr
    assert "super-secret-value" not in node.receipt.read_text(encoding="utf-8")


def test_the_script_never_prints_a_token_from_the_environment(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main", LH_HARNESS_WEB_TOKEN="super-secret-value")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "super-secret-value" not in result.stdout
    assert "super-secret-value" not in result.stderr


def test_a_dry_run_touches_nothing(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main", "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (node.root / "restart-count").read_text().strip() == "0"
    assert not node.receipt.exists()
    assert "# old" in node.installed_marker()


def test_an_unresolvable_ref_fails_loudly(node: Node) -> None:
    result = node.run("--ref", "no-such-ref")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not resolve" in result.stderr


# --- the privilege preflight ------------------------------------------------


def test_the_preflight_reports_a_clear_error_when_the_venv_is_not_writable(
    node: Node,
) -> None:
    """The venv is root-owned on a node; the deploy must say so plainly.

    The deploy writes three things: the venv (root-owned on a node), the
    state directory (owner harness) and settings.db (owner harness).  When
    the invoking user can write none of them the deploy must fail with a
    clear message BEFORE it touches anything -- not with an opaque pip error
    half way through the install.
    """
    # Make the venv unwritable for the current (non-root) user.
    venv_root = node.venv
    original_mode = venv_root.stat().st_mode
    venv_root.chmod(0o555)
    try:
        result = node.run("--ref", "main")

        assert result.returncode == 1, result.stdout + result.stderr
        assert "not writable" in result.stderr, result.stderr
        # The failure names the path and the user it is running as.
        assert str(node.venv) in result.stderr, result.stderr
        assert "harness" in result.stderr, result.stderr
        # Nothing was installed, restarted or written.
        assert (node.root / "restart-count").read_text().strip() == "0"
        assert not node.receipt.exists()
    finally:
        venv_root.chmod(original_mode)


def test_the_preflight_passes_when_the_paths_are_writable(node: Node) -> None:
    (node.root / "meta-commit").write_text(node.new_sha, encoding="utf-8")

    result = node.run("--ref", "main")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "privilege preflight" in result.stdout, result.stdout
