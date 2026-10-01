"""Deploy targets: one workflow, several nodes (ct110, ct111).

``.github/workflows/deploy-ct110.yml`` deploys the node named by its ``target``
input.  These tests pin the two properties that matter for a production
deploy pipeline:

* a dispatch WITHOUT ``target`` (or with ``target=ct110``) resolves to exactly
  the CT110 environment, secrets, variables, concurrency group, artifact name
  and job set the workflow had before targets existed;
* ``target=ct111`` resolves to CT111's own names and can never fall through
  to a CT110 credential or URL (and the reverse).

The workflow's per-target expressions are evaluated here with a small
stand-in for the GitHub expression language (the subset the workflow uses), so
the assertions are about what the expressions RESOLVE to, not about their text.
``scripts/deploy/nodes.json`` is the inventory; the workflow does not read it,
and these tests are what keeps the two in agreement.

The new-node first-install files (``scripts/deploy/node/``) are checked here
too: secret hygiene of the unit template, the node config template loading
through the harness's own config loader, and shell syntax.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy-ct110.yml"
NODES = REPO_ROOT / "scripts" / "deploy" / "nodes.json"
NODE_DIR = REPO_ROOT / "scripts" / "deploy" / "node"
PREFLIGHT = REPO_ROOT / "scripts" / "deploy" / "ct110" / "preflight.py"
SECRET_SCAN = REPO_ROOT / "scripts" / "deploy" / "secret_scan.sh"

CT110_LAN_URL = "http://192.168.21.168:8799"
EXPR = re.compile(r"\$\{\{(.*?)\}\}")


class _Context(dict):
    """``secrets`` / ``vars``: a missing name is empty, as in GitHub Actions."""

    def __missing__(self, key: str) -> str:
        return ""


def _evaluate(expression: str, *, target: str | None, secrets=None, variables=None,
              dry_run: bool = False, deploy_result: str = "success"):
    """Evaluate one GitHub expression (the subset this workflow uses).

    ``&&`` / ``||`` return an operand, with '' / null / false falsy — the same
    as Python's ``and`` / ``or`` — and ``format('{0}..', x)`` matches
    ``str.format``.  ``target=None`` models a dispatch that does not pass the
    input: GitHub then supplies the declared default.
    """
    python = expression.strip().replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"!(?!=)", " not ", python)
    scope = {
        "inputs": SimpleNamespace(target=_default_target() if target is None else target,
                                  dry_run=dry_run),
        "secrets": _Context(secrets or {}),
        "vars": _Context(variables or {}),
        "needs": SimpleNamespace(deploy=SimpleNamespace(result=deploy_result)),
        "format": lambda template, *args: template.format(*args),
    }
    return eval(python, {"__builtins__": {}}, scope)  # noqa: S307 - repo-owned text


def _render(value: str, **context) -> str:
    """Render a YAML scalar that embeds ``${{ }}`` expressions."""

    def substitute(match: re.Match[str]) -> str:
        result = _evaluate(match.group(1), **context)
        return "" if result is None or result is False else str(result)

    return EXPR.sub(substitute, value)


def _workflow_lines() -> list[str]:
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def _values(prefix: str) -> list[str]:
    """The ``key: value`` VALUE of every line whose stripped text starts with ``prefix``."""
    return [line.strip().split(":", 1)[1].strip() for line in _workflow_lines()
            if line.strip().startswith(prefix)]


def _value(prefix: str) -> str:
    found = _values(prefix)
    assert len(found) == 1, f"expected exactly one {prefix!r} line, found {len(found)}"
    return found[0]


def _nodes() -> dict:
    return json.loads(NODES.read_text(encoding="utf-8"))


def _default_target() -> str:
    block = WORKFLOW.read_text(encoding="utf-8").split("      target:\n", 1)[1]
    block = block.split("      target_ref:", 1)[0]
    return re.search(r"^\s+default:\s*(\S+)\s*$", block, re.MULTILINE).group(1)


def _target_options() -> list[str]:
    block = WORKFLOW.read_text(encoding="utf-8").split("      target:\n", 1)[1]
    block = block.split("      target_ref:", 1)[0]
    return re.findall(r"^\s+-\s+(\S+)\s*$", block, re.MULTILINE)


def _resolved(target: str | None, *, secrets=None, variables=None) -> dict[str, str]:
    """Everything the deploy job resolves per target."""
    context = {"target": target, "secrets": secrets, "variables": variables}
    artifact = {_render(v, **context) for v in _values("name: ${{")}
    assert len(artifact) == 1, "upload and download must name the same artifact"
    return {
        "environment": _render(_value("environment: ${{"), **context),
        "concurrency": _render(_value("group:"), **context),
        "artifact": artifact.pop(),
        "job_name": _render(_value("name: Deploy to"), **context),
        "target_node": _render(_value("TARGET_NODE:"), **context),
        "api_token": _render(_value("CT110_API_TOKEN: ${{"), **context),
        "ssh_key": _render(_value("CT110_PVE_SSH_KEY: ${{"), **context),
        "web_url": _render(_value("CT110_WEB_URL: ${{"), **context),
        "pve_host": _render(_value("PVE_HOST: ${{"), **context),
        "pve_user": _render(_value("PVE_USER: ${{"), **context),
        "ct_id": _render(_value("CT_ID: ${{"), **context),
    }


ALL_SECRETS = {
    "CT110_API_TOKEN": "<ct110-token>", "CT110_PVE_SSH_KEY": "<ct110-key>",
    "CT111_API_TOKEN": "<ct111-token>", "CT111_PVE_SSH_KEY": "<ct111-key>",
}


# --- inventory <-> workflow -------------------------------------------------

def test_inventory_lists_both_nodes_and_matches_the_target_input() -> None:
    inventory = _nodes()
    assert set(inventory["nodes"]) == {"ct110", "ct111"}
    assert _target_options() == list(inventory["nodes"])
    assert _default_target() == inventory["defaultTarget"] == "ct110"


def test_inventory_carries_names_only_and_no_address_for_ct111() -> None:
    ct111 = _nodes()["nodes"]["ct111"]
    assert ct111["ctId"] == 111 and ct111["hostname"] == "cct-cfo-harness-01"
    assert ct111["overseerUnits"] is False
    assert ct111["workflowDefaults"]["webUrl"] is None, "ct111 must have no default URL"
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", json.dumps(ct111)), (
        "the ct111 inventory entry must not contain an IP address — only the var NAME"
    )
    for node in _nodes()["nodes"].values():
        for name in [*node["secrets"].values(), *node["vars"].values()]:
            assert re.fullmatch(r"CT\d+_[A-Z_]+", name), f"{name!r} is not a NAME"


@pytest.mark.parametrize("node_id", ["ct110", "ct111"])
def test_workflow_resolves_each_target_to_its_inventory_entry(node_id: str) -> None:
    node = _nodes()["nodes"][node_id]
    secrets = {node["secrets"]["apiToken"]: "<token>", node["secrets"]["pveSshKey"]: "<key>"}
    variables = {node["vars"][key]: f"<{key}>" for key in node["vars"]}

    resolved = _resolved(node_id, secrets=secrets, variables=variables)
    assert resolved["environment"] == node["githubEnvironment"]
    assert resolved["concurrency"] == node["concurrencyGroup"]
    assert resolved["artifact"] == node["wheelArtifact"]
    assert resolved["target_node"] == node_id
    # Only this node's secret/variable NAMES were populated, and all resolved.
    assert (resolved["api_token"], resolved["ssh_key"]) == ("<token>", "<key>")
    assert resolved["web_url"] == "<webUrl>"
    assert resolved["pve_host"] == "<pveHost>"
    assert resolved["pve_user"] == "<pveSshUser>"
    assert resolved["ct_id"] == "<ctId>"

    defaults = _resolved(node_id, secrets=secrets)
    expected = node["workflowDefaults"]
    assert defaults["web_url"] == (expected["webUrl"] or "")
    assert defaults["pve_host"] == expected["pveHost"]
    assert defaults["pve_user"] == expected["pveSshUser"]
    assert defaults["ct_id"] == expected["ctId"]


# --- CT110 behaviour is unchanged -------------------------------------------

@pytest.mark.parametrize("target", [None, "ct110"])
def test_dispatch_without_target_is_the_original_ct110_deploy(target: str | None) -> None:
    """The values the workflow hard-coded before targets existed."""
    resolved = _resolved(target, secrets=ALL_SECRETS)
    assert resolved == {
        "environment": "ct110-prod",
        "concurrency": "deploy-ct110",
        "artifact": "ct110-wheel",
        "job_name": "Deploy to CT110 (zero-active window, bytes-safe, verified)",
        "target_node": "ct110",
        "api_token": "<ct110-token>",
        "ssh_key": "<ct110-key>",
        "web_url": CT110_LAN_URL,
        "pve_host": "corsairai300",
        "pve_user": "root",
        "ct_id": "110",
    }
    overridden = _resolved(target, secrets=ALL_SECRETS, variables={
        "CT110_WEB_URL": "http://ct110.example:8799", "CT110_PVE_HOST": "pve-a",
        "CT110_PVE_SSH_USER": "deploy", "CT110_CT_ID": "910",
        # CT111 variables must be ignored for a CT110 deploy.
        "CT111_WEB_URL": "http://ct111.example:8799", "CT111_CT_ID": "911",
    })
    assert (overridden["web_url"], overridden["pve_host"], overridden["pve_user"],
            overridden["ct_id"]) == ("http://ct110.example:8799", "pve-a", "deploy", "910")


@pytest.mark.parametrize("target", [None, "ct110"])
def test_overseer_units_job_still_runs_for_ct110_only(target: str | None) -> None:
    condition = _value("if: ${{ !inputs.dry_run && needs.deploy.result")
    assert _evaluate(EXPR.search(condition).group(1), target=target) is True
    assert _evaluate(EXPR.search(condition).group(1), target=target, dry_run=True) is False
    assert _evaluate(EXPR.search(condition).group(1), target=target,
                     deploy_result="failure") is False
    assert _evaluate(EXPR.search(condition).group(1), target="ct111") is False
    # The job itself still names CT110's environment literally.
    assert "environment: ct110-prod" in [line.strip() for line in _workflow_lines()]


def test_ct111_only_guard_step_is_skipped_for_ct110() -> None:
    condition = EXPR.search(_value("if: ${{ inputs.target ==")).group(1)
    assert _evaluate(condition, target=None) is False
    assert _evaluate(condition, target="ct110") is False
    assert _evaluate(condition, target="ct111") is True


# --- no cross-node fall-through ---------------------------------------------

def test_ct111_never_falls_through_to_a_ct110_credential_or_url() -> None:
    only_ct110 = _resolved(
        "ct111",
        secrets={"CT110_API_TOKEN": "<ct110-token>", "CT110_PVE_SSH_KEY": "<ct110-key>"},
        variables={"CT110_WEB_URL": "http://ct110.example:8799", "CT110_CT_ID": "110"},
    )
    assert only_ct110["environment"] == "ct111-prod"
    assert only_ct110["concurrency"] == "deploy-ct111"
    assert only_ct110["api_token"] == "" and only_ct110["ssh_key"] == ""
    assert only_ct110["web_url"] == "", "ct111 has no default URL and must not borrow CT110's"
    assert only_ct110["ct_id"] == "111"


def test_ct110_never_falls_through_to_a_ct111_credential() -> None:
    only_ct111 = _resolved(
        "ct110",
        secrets={"CT111_API_TOKEN": "<ct111-token>", "CT111_PVE_SSH_KEY": "<ct111-key>"},
    )
    assert only_ct111["api_token"] == "" and only_ct111["ssh_key"] == ""


def test_secrets_are_selected_by_built_name_not_by_ternary() -> None:
    """``a && secrets.X || secrets.Y`` leaks Y when X is unset; forbid the shape."""
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert not re.search(r"&&\s*secrets\.", code)
    assert not re.search(r"\|\|\s*secrets\.", code)


def test_preflight_ignores_the_deploy_job_under_either_target_name() -> None:
    spec = importlib.util.spec_from_file_location("ct110_preflight", PREFLIGHT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for target in ("ct110", "ct111"):
        job_name = _resolved(target)["job_name"]
        assert job_name.startswith(module.DEPLOY_JOB_PREFIXES), job_name


# --- new-node first install (scripts/deploy/node) ---------------------------

def test_node_unit_template_is_environment_file_only() -> None:
    text = (NODE_DIR / "templates" / "lh-harness-node.service").read_text(encoding="utf-8")
    service = text.split("[Service]", 1)[1]
    assert "EnvironmentFile=/home/harness/.lh-harness-secrets.env" in service
    assert not [line for line in text.splitlines() if line.strip().startswith("Environment=")]
    exec_start = next(line for line in service.splitlines() if line.startswith("ExecStart="))
    assert exec_start.startswith("ExecStart=/home/harness/venv/bin/python -m lh_harness web ")
    assert "--host 0.0.0.0" in exec_start
    assert "--workspace-root @WORKSPACE_ROOT@" in exec_start
    assert "OOMPolicy=continue" in service and "Delegate=memory pids" in service
    assert not re.search(r"\b[0-9a-f]{48}\b", text)


def test_ct111_config_template_loads_through_the_harness_config_loader(tmp_path: Path) -> None:
    from lh_harness.config import load_run_defaults
    from lh_harness.mcp_profiles import resolve_profile
    from lh_harness.queue import queue_config_from_config

    template = (NODE_DIR / "templates" / "config.ct111.toml").read_text(encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(template.replace("@RUNS_ROOT@", "/var/lib/lh-harness/runs"),
                      encoding="utf-8")

    defaults = load_run_defaults(config)
    assert defaults["runs_root"] == "/var/lib/lh-harness/runs"
    queue = queue_config_from_config(defaults)
    assert set(queue["trios"]) == {"kimi", "qwen"}
    assert {name: trio["mcp_profile"] for name, trio in queue["trios"].items()} == {
        "kimi": "finance", "qwen": "finance"}
    assert (queue["capacity"]["kimi_max"], queue["capacity"]["qwen_max"]) == (2, 1)

    finance = resolve_profile("executor", role_profile="finance",
                              project_config_path=config, gateway_key="validation-only")
    assert finance.name == "finance" and finance.source == "project"
    assert finance.servers == ("kb", "guides", "skills", "memory", "quickbooks", "stripe")
    # The auditor binding is what keeps the launcher from refusing every entry.
    assert defaults["auditor_mcp_profile"] == "finance_audit"
    auditor = resolve_profile("auditor", role_profile=defaults["auditor_mcp_profile"],
                              project_config_path=config, gateway_key="validation-only")
    assert auditor.name == "finance_audit" and auditor.read_only is True
    # ...because the trio-wide profile is NOT read-only (the supervisor and the
    # launcher's pre-burn check refuse an auditor that resolves to it).
    assert finance.read_only is False


def test_node_scripts_are_lf_and_parse() -> None:
    bash = shutil.which("bash")
    scripts = sorted(NODE_DIR.glob("*.sh"))
    assert [s.name for s in scripts] == ["bootstrap-node.sh", "pve-create-node.sh"]
    for path in [*scripts, *sorted((NODE_DIR / "templates").iterdir())]:
        assert b"\r" not in path.read_bytes(), f"{path.name} carries CR bytes"
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "set -euo pipefail" in text and "set -x" not in text
        if bash:
            result = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True,
                                    timeout=60)
            assert result.returncode == 0, result.stderr


def test_bootstrap_never_overwrites_operator_owned_files_or_inlines_a_credential() -> None:
    text = (NODE_DIR / "bootstrap-node.sh").read_text(encoding="utf-8")
    # Admin-supplied credentials are written as EMPTY placeholders.
    for name in ("LH_HARNESS_MCP_GATEWAY_KEY", "GH_TOKEN", "LH_HARNESS_FLEET_KEY"):
        assert f'echo "{name}="\n' in text, f"{name} must be an empty placeholder"
    # The generated web token goes straight into the file, never through echo/log.
    assert "printf 'LH_HARNESS_WEB_TOKEN=%s\\n'" in text
    assert not re.search(r"(echo|log)[^\n]*\$\(env_value LH_HARNESS_WEB_TOKEN\)", text)
    for guarded in ('if [ -f "$config_path" ]', 'if [ ! -f "$profiles_path" ]',
                    'if [ -f "$ENV_FILE" ]'):
        assert guarded in text


def test_secret_scan_covers_the_node_directory() -> None:
    assert '"$LH_SECRET_SCAN_ROOT/scripts/deploy/node"' in SECRET_SCAN.read_text(encoding="utf-8")
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    result = subprocess.run([bash, str(SECRET_SCAN)], cwd=str(REPO_ROOT), capture_output=True,
                            text=True, timeout=120)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
