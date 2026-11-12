"""The node deploy workflow: the ref-deploy pipeline and its reuse of the
existing CT202-style deploy path.

``.github/workflows/deploy-harness-node.yml`` installs a node from a git REF.
It must reuse the deploy path this repository already has -- the self-hosted
``lan-deploy`` runner, the per-node SSH key secret, the Proxmox host and the
container id, and ``scripts/deploy/ct110/run_on_ct110.sh`` for the transfer --
rather than inventing a second one, and it must never carry a credential
value.

These tests read the workflow and the scripts as text (and parse the YAML
when pyyaml is available); none of them touches a node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-harness-node.yml"
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "deploy-node.sh"
RUNNER_SCRIPT = REPO_ROOT / "scripts" / "deploy" / "ct110" / "run_on_ct110.sh"
CT110_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-ct110.yml"
NODES = REPO_ROOT / "scripts" / "deploy" / "nodes.json"
SECRET_SCAN = REPO_ROOT / "scripts" / "deploy" / "secret_scan.sh"
DOC = REPO_ROOT / "docs" / "deploy" / "harness-node.md"

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="the node deploy path needs bash",
)


def _yaml() -> object:
    yaml = pytest.importorskip(
        "yaml", reason="pyyaml is not a declared test extra; the workspace venv provides it"
    )
    return yaml


# --- the workflow's shape ----------------------------------------------------


def test_the_workflow_parses_and_is_dispatch_only() -> None:
    yaml = _yaml()
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), triggers
    assert set(triggers) == {"workflow_dispatch"}, triggers
    assert "push" not in triggers and "schedule" not in triggers


def test_the_dispatch_takes_the_node_and_the_ref() -> None:
    yaml = _yaml()
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get("on", doc.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"node", "ref", "drain_wait_seconds", "dry_run"}, sorted(inputs)
    assert inputs["node"]["type"] == "choice"
    assert inputs["node"]["options"] == ["ct110", "ct111"]
    assert inputs["node"]["default"] == "ct110"
    assert inputs["ref"]["required"] is True
    assert inputs["ref"]["default"] == "origin/main"


def test_the_workflow_runs_on_the_existing_self_hosted_runner() -> None:
    yaml = _yaml()
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # The same runner label the CT110 deploy uses: one deploy path, not two.
    assert doc["jobs"]["deploy"]["runs-on"] == ["self-hosted", "lan-deploy"]
    ct110 = yaml.safe_load(CT110_WORKFLOW.read_text(encoding="utf-8"))
    labels = {
        tuple(job["runs-on"])
        for job in ct110["jobs"].values()
        if isinstance(job.get("runs-on"), list)
    }
    assert ("self-hosted", "lan-deploy") in labels


def test_the_workflow_uses_the_per_node_environment_and_secret_names() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # Secret and variable NAMES are selected from a literal prefix, so an
    # unset secret resolves to empty instead of falling through to the other
    # node's credential.
    assert "secrets[format('{0}_API_TOKEN'" in text
    assert "secrets[format('{0}_PVE_SSH_KEY'" in text
    assert "vars[format('{0}_WEB_URL'" in text
    assert "vars[format('{0}_PVE_HOST'" in text
    assert "vars[format('{0}_CT_ID'" in text
    # The environment per node is the one the inventory already names.
    assert "ct110-prod" in text and "ct111-prod" in text


def test_the_workflow_carries_no_credential_value() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # No literal token: every credential is a `secrets[...]` lookup.
    assert "secrets.CT110_API_TOKEN" not in text
    assert "secrets.CT111_API_TOKEN" not in text
    assert re.search(r"ghp_[A-Za-z0-9]{20,}", text) is None
    assert re.search(r"github_pat_[A-Za-z0-9_]{20,}", text) is None
    assert re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text) is None


def test_the_workflow_never_mentions_ptait09() -> None:
    assert "ptait09" not in WORKFLOW.read_text(encoding="utf-8").lower()


def test_the_workflow_uses_the_shared_runner_side_transfer() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    # The transfer is the existing one, not a second implementation.
    assert "scripts/deploy/ct110/run_on_ct110.sh" in text
    assert "run_on_ct110.sh script stage/deploy-node.sh" in text


def test_the_workflow_verifies_the_receipt_on_the_node() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "/home/harness/.lh-harness/deployed.json" in text
    assert "DEPLOY_OK" in text or "HARNESS_NODE_DEPLOY_OK" in text


# --- the scripts -------------------------------------------------------------


def test_the_deploy_script_parses_and_is_executable() -> None:
    assert DEPLOY_SCRIPT.is_file()
    result = subprocess.run(["bash", "-n", str(DEPLOY_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert DEPLOY_SCRIPT.stat().st_mode & 0o111, "deploy-node.sh must be executable"


def test_the_runner_side_script_parses() -> None:
    result = subprocess.run(["bash", "-n", str(RUNNER_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_runner_side_script_gained_a_script_mode() -> None:
    text = RUNNER_SCRIPT.read_text(encoding="utf-8")
    assert 'run_on_ct110.sh script <file> [args...]' in text
    # The mode stages the payload through the SAME byte-verified path.
    assert '"$MODE" == "script"' in text
    assert "hop1 ok" in text and "hop2 ok" in text


def test_the_deploy_script_takes_a_ref_and_a_drain_wait() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert 'REF="${LH_DEPLOY_REF:-origin/main}"' in text
    assert "--drain-wait" in text
    assert "--dry-run" in text


def test_the_deploy_script_reads_the_control_status_files() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # The drain rule reads the run state the supervisor writes.
    assert '"control"' in text and '"status.json"' in text
    # The ACTIVE vocabulary of src/lh_harness/supervisor/lifecycle.py.
    for status in ("creating", "starting", "running", "waiting_approval", "stopping"):
        assert status in text


def test_the_deploy_script_writes_the_receipt_with_the_required_fields() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "deployed.json" in text
    for field in ('"commit"', '"version"', '"at"', '"by"', '"previous"'):
        assert field in text, field


def test_the_deploy_script_never_opens_the_secrets_file() -> None:
    # The rule is that the deploy does not touch the credentials at all.  The
    # file may be NAMED in a comment (the script explains that it does not
    # read it), but no COMMAND may read, open or copy it.
    code_lines = []
    for line in DEPLOY_SCRIPT.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    assert ".lh-harness-secrets.env" not in code
    assert "SECRETS_FILE" not in code
    # The token is only ever SENT to the local API, never read from a file.
    assert "LH_HARNESS_WEB_TOKEN" in code


def test_the_deploy_script_does_not_print_a_secret() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    # The token may be SENT to the local API, never echoed.
    for pattern in (r'echo\s+.*\$\{?LH_HARNESS_WEB_TOKEN', r'printf\s+.*\$\{?LH_HARNESS_WEB_TOKEN'):
        assert re.search(pattern, text) is None, pattern


def test_the_deploy_script_rolls_back_on_a_failed_read_back() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    assert "rollback" in text
    for marker in ("/api/health", "/api/meta", "is-active"):
        assert marker in text, marker


def test_the_secret_scan_passes_over_the_new_script() -> None:
    result = subprocess.run(
        ["bash", str(SECRET_SCAN), str(DEPLOY_SCRIPT), str(RUNNER_SCRIPT)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- the docs ----------------------------------------------------------------


def test_the_doc_covers_the_operator_steps_the_drain_rule_and_the_receipt() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert DOC.is_file()
    # Operator steps.
    assert "gh workflow run deploy-harness-node.yml" in text
    assert "deploy-node.sh --ref" in text
    # The drain rule.
    assert "--drain-wait" in text
    assert "waiting_approval" in text
    # The receipt format.
    for field in ('"commit"', '"version"', '"at"', '"by"', '"previous"'):
        assert field in text, field
    assert "DEPLOY_OK" in text


def test_the_doc_never_mentions_ptait09() -> None:
    assert "ptait09" not in DOC.read_text(encoding="utf-8").lower()


# --- the inventory -----------------------------------------------------------


def test_the_workflow_only_ships_nodes_the_inventory_knows() -> None:
    inventory = json.loads(NODES.read_text(encoding="utf-8"))
    known = set(inventory["nodes"])
    yaml = _yaml()
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get("on", doc.get(True))
    options = set(triggers["workflow_dispatch"]["inputs"]["node"]["options"])
    assert options <= known, (options, known)
