"""scripts/deploy/ct110/ct110_deploy.sh: deploy and rollback track the BUILD, not the version.

Deploy run 37162090599 (b6f51f9, LHH#97) "rolled back" to 0.1.7 by reinstalling
``wheels/lh_harness-0.1.7-py3-none-any.whl`` -- the file the same deploy had
just overwritten with the NEW wheel, because every build is version 0.1.7.  The
new code kept running while the run reported a rollback.

These tests drive the real script against a throwaway venv holding a fake
``lh-harness`` 0.1.7 package (two builds that differ only by commit), with
``systemctl`` and ``curl`` shimmed on PATH.  They prove that rollback restores
the pre-deploy bytes and commit, and that it reports honestly when it cannot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "deploy" / "ct110" / "ct110_deploy.sh"
BUILD_INFO_SRC = (REPO_ROOT / "src" / "lh_harness" / "build_info.py").read_text(encoding="utf-8")
VERSION = "0.1.7"
OLD = "a" * 40
NEW = "b" * 40

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None or shutil.which("sha256sum") is None,
    reason="ct110_deploy.sh needs bash and coreutils",
)


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def _build_wheel(out_dir: Path, commit: str | None) -> Path:
    """A minimal pure-Python wheel named exactly like the real one."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dist_info = f"lh_harness-{VERSION}.dist-info"
    files: dict[str, bytes] = {
        "lh_harness/__init__.py": f'__version__ = "{VERSION}"\n'.encode(),
        f"{dist_info}/METADATA": f"Metadata-Version: 2.1\nName: lh-harness\nVersion: {VERSION}\n".encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if commit is not None:  # None = a build that predates build info
        files["lh_harness/build_info.py"] = BUILD_INFO_SRC.encode()
        files["lh_harness/_build_info.json"] = json.dumps({"commit": commit}).encode()
    record = "".join(f"{name},{_record_hash(data)},{len(data)}\n" for name, data in files.items())
    record += f"{dist_info}/RECORD,,\n"
    files[f"{dist_info}/RECORD"] = record.encode()
    wheel = out_dir / f"lh_harness-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return wheel


def _make_venv(path: Path) -> None:
    try:
        venv.create(path, with_pip=True)
        return
    except (Exception, SystemExit):  # venv exits 1 when ensurepip is missing
        shutil.rmtree(path, ignore_errors=True)
    # No ensurepip on this interpreter: drive the test runner's own pip at the
    # new venv with `pip --python` (pip >= 22.3).
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


class Host:
    """A fake CT110: venv, wheel archive, state dir, shims; runs the script."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.venv = root / "venv"
        self.archive = root / "wheels"
        self.state = root / "state"
        self.deploy_dir = root / "lh-deploy"
        self.hold = root / "DEPLOY-HOLD"
        shims = root / "shims"
        shims.mkdir()
        (shims / "systemctl").write_text(
            '#!/usr/bin/env bash\n[ "$1" = is-active ] && echo active\nexit 0\n', encoding="utf-8")
        (shims / "curl").write_text("#!/usr/bin/env bash\necho '{\"runs\": []}'\n", encoding="utf-8")
        for shim in shims.iterdir():
            shim.chmod(0o755)
        secrets = root / "secrets.env"
        secrets.write_text("LH_HARNESS_WEB_TOKEN=test-only\n", encoding="utf-8")
        self.env = {
            **os.environ,
            "PATH": f"{shims}{os.pathsep}{os.environ.get('PATH', '')}",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "LH_DEPLOY_DIR": str(self.deploy_dir),
            "LH_WHEEL_ARCHIVE": str(self.archive),
            "LH_DEPLOY_STATE": str(self.state),
            "LH_VENV": str(self.venv),
            "LH_SECRETS_FILE": str(secrets),
            "LH_DEPLOY_HOLD_FILE": str(self.hold),
            "LH_EXPECTED_VERSION": VERSION,
        }
        self.env.pop("PYTHONPATH", None)
        _make_venv(self.venv)

    def pip_install(self, wheel: Path) -> None:
        subprocess.run([str(self.venv / "bin" / "pip"), "install", "--force-reinstall", "--no-deps", str(wheel)],
                       env=self.env, check=True, capture_output=True, text=True)

    def stage(self, wheel: Path) -> None:
        shutil.rmtree(self.deploy_dir, ignore_errors=True)
        self.deploy_dir.mkdir(parents=True)
        shutil.copy2(wheel, self.deploy_dir / wheel.name)

    def run(self, mode: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SCRIPT), mode], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=300)

    def commit(self) -> str:
        out = subprocess.run([str(self.venv / "bin" / "python"), "-m", "lh_harness.build_info"],
                             env=self.env, capture_output=True, text=True)
        return out.stdout.strip() if out.returncode == 0 else ""

    def wheel_sha(self) -> str:
        code = ("import json; from importlib.metadata import distribution; "
                "print(json.loads(distribution('lh-harness').read_text('direct_url.json'))"
                "['archive_info']['hashes']['sha256'])")
        return subprocess.run([str(self.venv / "bin" / "python"), "-c", code], env=self.env,
                              capture_output=True, text=True, check=True).stdout.strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


def _seed_legacy(host: Host, commit: str | None) -> Path:
    """Today's CT110: the live wheel sits in the archive under its flat version name."""
    old = _build_wheel(host.archive, commit)
    host.pip_install(old)
    return old


def test_same_version_rollback_restores_the_previous_commit(host: Host, tmp_path: Path) -> None:
    old = _seed_legacy(host, OLD)
    old_sha = _sha(old)
    new = _build_wheel(tmp_path / "build-new", NEW)
    host.stage(new)

    deployed = host.run("deploy", LH_EXPECTED_COMMIT=NEW)
    assert deployed.returncode == 0, deployed.stderr
    assert f"CT110_DEPLOY_INNER_OK version={VERSION} commit={NEW}" in deployed.stdout
    assert host.commit() == NEW
    assert (host.archive / "by-commit" / NEW / new.name).is_file()

    # What the pre-fix deploy did after its install: overwrite the flat,
    # version-named archive file with the NEW wheel.  Rollback must not care.
    shutil.copy2(new, host.archive / new.name)

    rolled = host.run("rollback")
    assert rolled.returncode == 0, rolled.stderr
    assert f"CT110_ROLLBACK_INNER_OK restored={VERSION} commit={OLD} wheel_sha256={old_sha}" in rolled.stdout
    assert host.commit() == OLD
    assert host.wheel_sha() == old_sha


def test_wrong_commit_in_the_staged_wheel_fails_the_deploy(host: Host, tmp_path: Path) -> None:
    _seed_legacy(host, OLD)
    host.stage(_build_wheel(tmp_path / "build-new", NEW))
    result = host.run("deploy", LH_EXPECTED_COMMIT="c" * 40)
    assert result.returncode == 4
    assert f"installed build commit is '{NEW}', expected {'c' * 40}" in result.stderr


def test_rollback_after_an_aborted_deploy_is_not_needed(host: Host, tmp_path: Path) -> None:
    _seed_legacy(host, OLD)
    # A finished earlier deploy left install_started behind; this one aborts.
    host.stage(_build_wheel(tmp_path / "build-new", NEW))
    assert host.run("deploy", LH_EXPECTED_COMMIT=NEW).returncode == 0
    assert host.run("rollback").returncode == 0
    host.hold.write_text("testing the hold\n", encoding="utf-8")

    aborted = host.run("deploy", LH_EXPECTED_COMMIT=NEW)
    assert aborted.returncode == 2
    assert "DEPLOY-HOLD present" in aborted.stderr

    rolled = host.run("rollback")
    assert rolled.returncode == 5
    assert "CT110_ROLLBACK_NOT_NEEDED" in rolled.stdout
    assert host.commit() == OLD


def test_rollback_without_a_copy_of_the_previous_wheel_fails_loudly(host: Host, tmp_path: Path) -> None:
    old = _seed_legacy(host, OLD)
    old.unlink()  # the live wheel was never archived
    host.stage(_build_wheel(tmp_path / "build-new", NEW))

    deployed = host.run("deploy", LH_EXPECTED_COMMIT=NEW)
    assert deployed.returncode == 0, deployed.stderr
    assert "a rollback of this deploy will be impossible" in deployed.stderr

    rolled = host.run("rollback")
    assert rolled.returncode == 3
    assert "no copy of the pre-deploy wheel" in rolled.stderr
    assert host.commit() == NEW  # still the new code: never claimed otherwise


def test_rollback_to_a_build_without_build_info_restores_its_bytes(host: Host, tmp_path: Path) -> None:
    old = _seed_legacy(host, None)  # what CT110 runs today (b6f51f9 predates build info)
    old_sha = _sha(old)
    host.stage(_build_wheel(tmp_path / "build-new", NEW))
    assert host.run("deploy", LH_EXPECTED_COMMIT=NEW).returncode == 0
    assert host.commit() == NEW

    rolled = host.run("rollback")
    assert rolled.returncode == 0, rolled.stderr
    assert f"commit=unknown wheel_sha256={old_sha}" in rolled.stdout
    assert host.commit() == ""
    assert host.wheel_sha() == old_sha
