"""Seeded L2/L3 defaults + the optional [experience] instance override.

The three levels must exist for every instance from the start (Paxton
2026-09-08): L3 genuinely populated as data, L2 an empty but addressable
collection in its final shape, L1 per run (covered by the trace slice
tests). These tests pin the envelope (origin/seeded_at/supersede fields),
the grounded fleet facts, the redaction choke point, and the override
semantics that must never disturb the run flow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lh_harness.config import ProjectConfigError, load_run_defaults
from lh_harness.experience import (
    ENVIRONMENT_KINDS,
    ORIGIN_SEEDED,
    POLICY_ITEM_SCHEMA,
    SEEDED_AT,
    SUMMARY_MAX_CHARS,
    SeededLevels,
    empty_policy_collection,
    load_seeded_levels,
    supersede,
)
from lh_harness.experience.redact import redact_text
from lh_harness.experience import seed as seed_module


def _by_id(levels: SeededLevels) -> dict[str, dict]:
    return {item["id"]: item for item in levels.environment}


def _load_pure(tmp_path: Path) -> SeededLevels:
    return load_seeded_levels(tmp_path / "does-not-exist.toml")


def test_pure_seeds_load_without_config(tmp_path: Path) -> None:
    levels = _load_pure(tmp_path)
    assert isinstance(levels, SeededLevels)
    assert isinstance(levels.environment, tuple)
    assert len(levels.environment) >= 10

    ids = [item["id"] for item in levels.environment]
    assert len(ids) == len(set(ids)), "seeded ids must be unique and quotable"
    for item in levels.environment:
        if item["kind"] != "note":
            assert item["id"].startswith("l3.")
        assert item["level"] == "L3"
        assert item["kind"] in ENVIRONMENT_KINDS
        assert item["origin"] == ORIGIN_SEEDED
        assert item["seeded_at"] == SEEDED_AT
        assert item["source"] == "code"
        assert item["superseded_by"] is None
        assert item["superseded_at"] is None
        assert item["summary"].strip()
        assert len(item["summary"]) <= SUMMARY_MAX_CHARS
        assert isinstance(item["detail"], dict)


def test_seeded_at_is_an_aware_iso_datetime(tmp_path: Path) -> None:
    levels = _load_pure(tmp_path)
    for item in levels.environment:
        stamped = datetime.fromisoformat(item["seeded_at"])
        assert stamped.tzinfo is not None


def test_l3_lists_the_fleet_hosts(tmp_path: Path) -> None:
    items = _by_id(_load_pure(tmp_path))
    hosts = {item["id"]: item for item in items.values() if item["kind"] == "host"}
    expected = {
        "l3.host.ct110",
        "l3.host.corsairai300",
        "l3.host.ct103",
        "l3.host.ptait01",
        "l3.host.ptait09",
        "l3.host.ct202",
        "l3.host.ptait-desk03",
        "l3.host.htpc01",
        "l3.host.ptait10am5",
    }
    assert expected <= set(hosts)
    ct110 = hosts["l3.host.ct110"]
    assert ct110["detail"]["hostname"] == "ct110"
    assert "external" in ct110["detail"]["purpose"]
    for host in hosts.values():
        assert host["detail"]["hostname"].strip()
        assert host["detail"]["purpose"].strip()
        assert isinstance(host["detail"]["can_run"], list)


def test_l3_standing_constraints_present(tmp_path: Path) -> None:
    items = _by_id(_load_pure(tmp_path))
    constraints = {k: v for k, v in items.items() if v["kind"] == "constraint"}
    expected = {
        "l3.constraint.host-change-explicit-go",
        "l3.constraint.runner-restart-from-outside",
        "l3.constraint.no-e2e-qa-on-dev-box",
        "l3.constraint.prod-via-lanes",
        "l3.constraint.some-hosts-no-ipv6-egress",
        "l3.constraint.stale-image-tag-rollback",
    }
    assert expected <= set(constraints)
    for constraint in constraints.values():
        assert constraint["detail"]["statement"].strip()


def test_l3_routing_backends_by_name_with_capacities_never_urls(
    tmp_path: Path,
) -> None:
    levels = _load_pure(tmp_path)
    backends = [item for item in levels.environment if item["kind"] == "routing_backend"]
    names = {item["detail"]["name"] for item in backends}
    assert names == {"local-span", "synthetic", "ollama-cloud"}
    caps = {item["detail"]["name"]: item["detail"]["max_concurrent"] for item in backends}
    # Grounded in the 2026-09-08 routing handoff: local parallelism is 2;
    # Synthetic is 2 concurrent per model per pack; Ollama Cloud has no
    # documented fixed concurrency cap (rate/monthly caps govern), so it
    # stays null rather than an invented number.
    assert caps["local-span"] == 2
    assert caps["synthetic"] == 2
    assert caps["ollama-cloud"] is None
    for backend in backends:
        assert "://" not in json.dumps(backend), "backend items must never carry URLs"


def test_l2_is_empty_but_addressable_with_final_shape(tmp_path: Path) -> None:
    levels = _load_pure(tmp_path)
    assert levels.policies == {}
    assert isinstance(levels.policies, dict)
    collection = empty_policy_collection()
    assert collection == {}
    # The final learned shape is declared as data so the empty state is not a
    # placeholder: the envelope fields match the L3 items, plus the L2 body a
    # generalized, device-requiring (never device-specific) routine will carry.
    assert POLICY_ITEM_SCHEMA["level"] == "'L2'"
    assert POLICY_ITEM_SCHEMA["origin"] == "'seeded' or 'learned'"
    assert "detail.device_requirement" in POLICY_ITEM_SCHEMA
    assert "superseded_by" in POLICY_ITEM_SCHEMA


def test_everything_json_round_trips(tmp_path: Path) -> None:
    levels = _load_pure(tmp_path)
    blob = json.dumps(levels.environment)
    assert json.loads(blob) == list(levels.environment)
    json.dumps(POLICY_ITEM_SCHEMA)


def test_seed_data_contains_nothing_redaction_would_strip(tmp_path: Path) -> None:
    levels = _load_pure(tmp_path)
    blob = json.dumps(levels.environment)
    # Hostnames/ports are fine; tokens/keys are not. A built-in seed carrying
    # a secret would fail this identity check.
    assert redact_text(blob) == blob


def test_module_constants_are_isolated_from_loaded_items(tmp_path: Path) -> None:
    first = _load_pure(tmp_path)
    first.environment[0]["detail"]["hostname"] = "corrupted"
    second = _load_pure(tmp_path)
    assert second.environment[0]["detail"]["hostname"] != "corrupted"


def test_load_is_deterministic_without_overrides(tmp_path: Path) -> None:
    assert _load_pure(tmp_path) == _load_pure(tmp_path)


def test_config_override_appends_items(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[run]
agent = "codex"

[[experience.environment]]
id = "l3.host.mynode"
kind = "host"
summary = "instance node with docker"
detail = {hostname = "mynode", can_run = ["bash", "docker heads"]}
""",
        encoding="utf-8",
    )
    levels = load_seeded_levels(config)
    items = _by_id(levels)
    override = items["l3.host.mynode"]
    assert override["origin"] == ORIGIN_SEEDED
    assert override["source"] == "config"
    assert override["detail"]["hostname"] == "mynode"
    stamped = datetime.fromisoformat(override["seeded_at"])
    assert stamped.tzinfo is not None
    # seeded_at for config items is the config file's mtime: when the
    # instance last recorded the knowledge.
    mtime = datetime.fromtimestamp(config.stat().st_mtime, tz=timezone.utc)
    assert abs((stamped - mtime).total_seconds()) < 1.0
    # The built-in seeds are untouched.
    assert "l3.host.ct110" in items


def test_override_supersedes_a_seed_without_deleting_it(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[experience.environment]]
id = "l3.backend.synthetic.packs-4"
kind = "routing_backend"
summary = "synthetic pack count raised to 4"
detail = {name = "synthetic", max_concurrent = 4}
supersedes = "l3.backend.synthetic"
""",
        encoding="utf-8",
    )
    levels = load_seeded_levels(config)
    items = _by_id(levels)
    seed = items["l3.backend.synthetic"]
    assert seed["superseded_by"] == "l3.backend.synthetic.packs-4"
    assert seed["superseded_at"] == items["l3.backend.synthetic.packs-4"]["seeded_at"]
    replacement = items["l3.backend.synthetic.packs-4"]
    assert replacement["detail"]["max_concurrent"] == 4
    assert replacement["source"] == "config"


def test_override_id_collision_without_supersedes_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[experience.environment]]
id = "l3.backend.synthetic"
kind = "routing_backend"
summary = "quietly overwrites the seed"
""",
        encoding="utf-8",
    )
    with pytest.raises(ProjectConfigError, match="collides"):
        load_seeded_levels(config)


def test_override_validation(tmp_path: Path) -> None:
    cases = [
        (  # missing id
            '[[experience.environment]]\nkind = "host"\nsummary = "x"\n',
            "id is required",
        ),
        (  # unknown kind
            '[[experience.environment]]\nid = "l3.x"\nkind = "mystery"\nsummary = "x"\n',
            "kind must be one of",
        ),
        (  # empty summary
            '[[experience.environment]]\nid = "l3.x"\nkind = "host"\nsummary = ""\n',
            "summary",
        ),
        (  # supersedes target does not exist
            '[[experience.environment]]\nid = "l3.x"\nkind = "note"\nsummary = "x"\nsupersedes = "l3.nope"\n',
            "does not match",
        ),
        (  # unknown key on an entry
            '[[experience.environment]]\nid = "l3.x"\nkind = "note"\nsummary = "x"\nextra = 1\n',
            "unknown key",
        ),
        (  # unknown key on the [experience] table
            '[experience]\nenvironment = []\nother = 1\n',
            "unknown key",
        ),
        (  # environment is not an array of tables
            '[experience]\nenvironment = "nope"\n',
            "array of tables",
        ),
    ]
    for body, fragment in cases:
        config = tmp_path / "config.toml"
        config.write_text(body, encoding="utf-8")
        with pytest.raises(ProjectConfigError, match=fragment):
            load_seeded_levels(config)
        config.unlink()


def test_redaction_applies_to_config_supplied_items(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[[experience.environment]]
id = "l3.note.instance"
kind = "note"
summary = "host mynode09 runs fine"
detail = {hostname = "mynode09", scratch = "key was sk-abcd1234abcd1234zz", token = "ghp_0123456789abcdef0123456789abcdef"}
""",
        encoding="utf-8",
    )
    levels = load_seeded_levels(config)
    item = _by_id(levels)["l3.note.instance"]
    blob = json.dumps(item)
    assert "sk-abcd1234abcd1234zz" not in blob
    assert "ghp_0123456789abcdef0123456789abcdef" not in blob
    assert "***REDACTED***" in blob
    # Hostnames and device ids are fine — redaction must not eat them.
    assert item["detail"]["hostname"] == "mynode09"
    assert "mynode09" in item["summary"]


def test_pure_constants_also_pass_redaction_when_secret_planted(tmp_path: Path) -> None:
    # _finalize is the choke point: even a module constant carrying a secret
    # is masked before it leaves the module, mirroring the trace rule.
    raw = {
        "id": "l3.note.planted",
        "kind": "note",
        "summary": "object STORAGE_PASSWORD=hunter22 here",
        "detail": {"pem": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"},
    }
    item = seed_module._finalize(dict(raw), seeded_at="2026-09-09T00:00:00+00:00", source="code")
    blob = json.dumps(item)
    assert "hunter22" not in blob
    assert "secret" not in blob


def test_override_does_not_change_l2_or_run_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """
[run]
max_rounds = 7
experience = true

[[experience.environment]]
id = "l3.note.extra"
kind = "note"
summary = "extra knowledge"
""",
        encoding="utf-8",
    )
    levels = load_seeded_levels(config)
    assert levels.policies == {}, "L2 is never seeded by configuration"
    # The [experience] table is not a run default: it must not land on the
    # flattened run config (run flow stays byte-identical), and the [run]
    # boolean that gates L1 capture is independent of it.
    defaults = load_run_defaults(config)
    assert defaults["max_rounds"] == 7
    assert defaults["experience"] is True
    assert "environment" not in repr(defaults)


def test_load_run_defaults_accepts_experience_top_level(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[experience]\n[run]\nagent = "codex"\n', encoding="utf-8")
    defaults = load_run_defaults(config)
    assert defaults["agent"] == "codex"
    assert "experience" not in defaults, "[experience] is not a run default"
    bad = tmp_path / "bad.toml"
    bad.write_text('experience = "yes"\n', encoding="utf-8")
    with pytest.raises(ProjectConfigError, match=r"\[experience\] must be a TOML table"):
        load_run_defaults(bad)


def test_supersede_helper() -> None:
    item = {"id": "l3.host.x", "superseded_by": None, "superseded_at": None, "other": 1}
    marked = supersede(item, replacement_id="l3.host.y", at="2026-09-09T00:00:00+00:00")
    assert marked["superseded_by"] == "l3.host.y"
    assert marked["superseded_at"] == "2026-09-09T00:00:00+00:00"
    assert item["superseded_by"] is None, "supersede copies; the original record is untouched"
    with pytest.raises(ValueError):
        supersede(item, replacement_id="l3.host.x", at="2026-09-09T00:00:00+00:00")
    with pytest.raises(ValueError):
        supersede(item, replacement_id="", at="2026-09-09T00:00:00+00:00")
    with pytest.raises(ValueError):
        supersede({"id": ""}, replacement_id="l3.host.y", at="2026-09-09T00:00:00+00:00")
