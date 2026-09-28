"""TASK 272: the shipped lh-harness unit template must never carry a SECRET VALUE.

The live unit on CT110 carried ``Environment=LH_HARNESS_WEB_TOKEN=<value>``
in plaintext (readable by anyone who could read the host filesystem via
``systemctl cat`` / the unit file itself; flagged by the task 262 audit and
hit again in a 2026-09-28 fleet check).  The source-side fix ships the
credential through the unit's ``EnvironmentFile`` instead:

    EnvironmentFile=/home/harness/.lh-harness-secrets.env

so the unit file itself stays world-readable AND value-free.  These tests
inspect the SHIPPED artifact — ``packaging/lh-harness.service``, the exact
file a deploy copies to ``/etc/systemd/system/lh-harness.service`` — so the
guard fires at the source, not after the leak is already on the host.

Companion guard: ``scripts/deploy/secret_scan.sh`` (same contract for the
whole ``packaging/`` + ``scripts/deploy/ct110/`` deploy surface).  The
module-level guard tests below run that script directly; the PR gate
(``pr-gate.yml``) runs plain pytest over the whole ``tests/`` tree, so
this module is exercised there without any extra gate wiring.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_TEMPLATE = REPO_ROOT / "packaging" / "lh-harness.service"
SECRET_SCAN = REPO_ROOT / "scripts" / "deploy" / "secret_scan.sh"

# The exact EnvironmentFile path the task contract pins (and the live unit
# already carries): the operator-owned, 0600, harness-owned secrets file.
ENVIRONMENT_FILE = "/home/harness/.lh-harness-secrets.env"

# A secret-bearing `Environment=` line: the task's own examples
# (LH_HARNESS_WEB_TOKEN) plus every credential-shaped NAME so a renamed
# credential cannot slip through.  Values are never captured — the regex
# stops at the NAME's `=`; nothing past it is matched or printed.
SECRET_ENV_NAME = re.compile(
    r"^[ \t]*Environment=([A-Za-z_][A-Za-z0-9_]*(_(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD)"
    r"|API_KEY|AUTH_TOKEN)|LH_HARNESS_WEB_TOKEN|CT110_API_TOKEN|GH_TOKEN)="
)

# docs/SECRETS.md pins the live CT110 harness API credential as a 48-char hex
# string.  A bare 48-hex run in a unit or deploy script is how the leak would
# look even if the NAME were renamed; git SHAs are 40-hex so 48 is not them.
HEX_CREDENTIAL_RE = re.compile(r"\b[0-9a-f]{48}\b")


def test_unit_template_has_no_nonsecret_environment_lines() -> None:
    """The template stays at its audited baseline: ZERO Environment= lines.

    round_001 measured the shipped template with no ``Environment=`` line at
    all (secrets AND non-secret topology both live outside it — the
    EnvironmentFile is the only environment mechanism it carries).  The
    "copy the unit" rollout step replaces the live unit's inline secret line
    with the EnvironmentFile; the live unit's non-secret topology lines stay
    host-side and must NOT be absorbed into the shipped template here, or
    the template would drift from the audited baseline.
    """
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    hits = [
        (no, line.strip())
        for no, line in enumerate(text.splitlines(), start=1)
        if line.strip().startswith("Environment=")
    ]
    assert not hits, (
        "shipped unit template carries Environment= line(s) that are not part "
        f"of its audited (round_001) baseline: {hits}"
    )


def test_unit_template_has_no_inline_secret_environment_line() -> None:
    """No `Environment=<credential NAME>=<value>` may exist in the shipped unit.

    The scan pattern must also survive a RENAMED credential (any *_TOKEN /
    *_KEY / *_SECRET / *_PASSWORD suffix), which is why the name match is
    suffix-driven rather than pinned to LH_HARNESS_WEB_TOKEN alone.
    """
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    hits = []
    for no, line in enumerate(text.splitlines(), start=1):
        match = SECRET_ENV_NAME.match(line)
        if match:
            hits.append((no, match.group(1)))
    assert not hits, (
        "shipped unit template carries an inline credential assignment — "
        "move the credential into /home/harness/.lh-harness-secrets.env "
        f"(EnvironmentFile) instead: lines {[no for no, _ in hits]}"
    )


def test_unit_template_references_the_environment_file() -> None:
    """The unit must source its credentials from the operator-owned file."""
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    assert f"EnvironmentFile={ENVIRONMENT_FILE}" in text, (
        "shipped unit template lost the EnvironmentFile reference — without it "
        "the service would start with NO web token and the API would lock out"
    )
    # ...and it must sit inside the [Service] section, where systemd applies it.
    service_section = text.split("[Service]", 1)[1]
    assert f"EnvironmentFile={ENVIRONMENT_FILE}" in service_section


def test_unit_template_does_not_embed_a_48_hex_credential() -> None:
    """The bearer credential is a 48-hex string (docs/SECRETS.md) — never ship it."""
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    hits = HEX_CREDENTIAL_RE.findall(text)
    assert not hits, (
        "shipped unit template carries a 48-hex credential-shaped literal "
        f"({len(hits)} occurrence(s)) — rotate it on the host and drop the line"
    )


def test_deploy_scripts_do_not_assign_a_credential_literal() -> None:
    """The CT110 deploy scripts must never hard-assign a credential value.

    ``ct110_deploy.sh`` reads LH_HARNESS_WEB_TOKEN from the EnvironmentFile
    (falling back to the legacy inline unit line until the rollout removes
    it) — reading by NAME is fine; ASSIGNING a literal is the leak.
    """
    deploy_dir = REPO_ROOT / "scripts" / "deploy" / "ct110"
    offenders: list[str] = []
    for script in sorted(deploy_dir.iterdir()):
        if script.suffix not in (".sh", ".py") or script.name.endswith(
            "secrets.env.example"
        ):
            continue
        for no, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
            match = re.match(
                r"^[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*"
                r"(?:_|API_KEY|AUTH_TOKEN|(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD))="
                r"[\"']?[A-Za-z0-9_/+-]{8,}",
                line,
            )
            if match:
                offenders.append(f"{script.name}:{no}")
    assert not offenders, (
        "credential-shaped literal assignment in deploy script(s): "
        f"{offenders} — read credentials from the EnvironmentFile by NAME"
    )


def test_secret_scan_guard_passes_on_the_shipped_tree() -> None:
    """The repo-side guard (packaging/ + scripts/deploy/ct110/) passes."""
    result = subprocess.run(
        ["bash", str(SECRET_SCAN)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"secret_scan found credential-shaped content:\n{result.stdout}\n{result.stderr}"
    )


def test_secret_scan_guard_fails_on_a_planted_leak(tmp_path: Path) -> None:
    """Counterfactual: the guard must FAIL when a leak is actually present.

    A guard that can never fail is not a guard.  This plants the exact leak
    shape task 272 found on CT110 into a scratch tree and requires the scan
    to flag it — with the VALUE redacted in the finding output.
    """
    leak = tmp_path / "leak.service"
    leak.write_text(
        "Environment=LH_HARNESS_WEB_TOKEN=000000000000000000000000000000000000000000000042\n",
        encoding="utf-8",
    )
    clean = tmp_path / "clean.service"
    clean.write_text(
        "EnvironmentFile=/home/harness/.lh-harness-secrets.env\n"
        "Environment=LH_OVERSEER_TICK_MODE=read-only\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(SECRET_SCAN), str(tmp_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "secret_scan passed on a tree carrying the 272 leak shape — the guard "
        "is not enforcing anything"
    )
    assert "leak.service" in result.stdout, result.stdout
    # The finding may carry the VARIABLE NAME (allowed); it must never carry
    # the planted value.
    assert "000000000000000000000000000000000000000000000042" not in result.stdout
    assert "000000000000000000000000000000000000000000000042" not in result.stderr


def test_secret_scan_guard_accepts_name_only_usage(tmp_path: Path) -> None:
    """Counterfactual (the other direction): NAME-only usage must NOT be flagged.

    The sanctioned shapes — EnvironmentFile references, `grep '^NAME='` of
    the secrets file, `${NAME}` expansions, empty-value example files — are
    the sanctioned mechanism; the guard must keep allowing them.
    """
    unit = tmp_path / "ok.service"
    unit.write_text(
        "# Secrets travel by NAME through the EnvironmentFile.\n"
        "EnvironmentFile=/home/harness/.lh-harness-secrets.env\n"
        "Environment=LH_OVERSEER_TICK_MODE=read-only\n",
        encoding="utf-8",
    )
    script = tmp_path / "ok.sh"
    script.write_text(
        'tok="$(grep -E \'^LH_HARNESS_WEB_TOKEN=\' /home/harness/.lh-harness-secrets.env '
        '| tail -n 1 | cut -d= -f2-)"\n'
        'curl -H "Authorization: Bearer ${CT110_API_TOKEN}" "$url"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(SECRET_SCAN), str(tmp_path)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"secret_scan flagged NAME-only credential usage:\n{result.stdout}\n{result.stderr}"
    )