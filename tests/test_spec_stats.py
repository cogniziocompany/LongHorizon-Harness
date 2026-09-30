from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from lh_harness import spec_stats


def _spec(status: str, body: str = "## Intent\nDo the thing.\n") -> str:
    return f"---\ntitle: t\nstatus: {status}\ncontext: [a.py, b.py]\n---\n{body}"


def test_parse_frontmatter_scalars_and_lists() -> None:
    meta, body = spec_stats.parse_frontmatter(_spec("draft"))
    assert meta["status"] == "draft"
    assert meta["context"] == ["a.py", "b.py"]
    assert body.startswith("## Intent")


def test_parse_frontmatter_absent() -> None:
    meta, body = spec_stats.parse_frontmatter("no frontmatter here")
    assert meta == {}
    assert body == "no frontmatter here"


def test_measure_spec_estimates_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = tmp_path / "s.md"
    path.write_text("x" * 401, encoding="utf-8")
    measure = spec_stats.measure_spec(path)
    assert measure.chars == 401
    assert measure.tokens_est == 101
    assert measure.exact is False


def test_range_unestablished_below_min_samples(tmp_path: Path) -> None:
    path = tmp_path / "spec_stats.json"
    stats = None
    for i in range(4):
        stats = spec_stats.record_finished(path, f"k{i}", 1000 + i)
    assert stats is not None
    assert stats.n == 4
    assert stats.established is False
    assert spec_stats.classify(50_000, stats) == "unestablished"


def test_range_median_mad_matches_hand_computation(tmp_path: Path) -> None:
    path = tmp_path / "spec_stats.json"
    values = [900, 1100, 1200, 1400, 1500, 1700, 2200]
    for i, v in enumerate(values):
        stats = spec_stats.record_finished(path, f"k{i}", v)
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    assert stats.established is True
    assert stats.median == median
    assert stats.mad == mad
    assert stats.upper == pytest.approx(median + 3 * 1.4826 * mad)
    assert stats.lower == pytest.approx(max(0.0, median - 3 * 1.4826 * mad))
    assert spec_stats.classify(int(median), stats) == "in_range"
    assert spec_stats.classify(int(stats.upper) + 1, stats) == "above"
    assert spec_stats.classify(int(stats.upper), stats) == "in_range"
    assert spec_stats.classify(max(0, int(stats.lower) - 1), stats) == ("below" if stats.lower > 0 else "in_range")


def test_outlier_does_not_pull_range_past_itself(tmp_path: Path) -> None:
    path = tmp_path / "spec_stats.json"
    for i in range(9):
        spec_stats.record_finished(path, f"k{i}", 1200 + (i % 3) * 50)
    stats = spec_stats.record_finished(path, "big", 12_000)
    assert stats.established
    assert spec_stats.classify(12_000, stats) == "above"


def test_window_truncates_and_rekey_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "spec_stats.json"
    for i in range(60):
        stats = spec_stats.record_finished(path, f"k{i}", 1000, config={"window": 50})
    assert stats.n == 50
    assert "k0" not in stats.samples and "k59" in stats.samples
    stats = spec_stats.record_finished(path, "k59", 2000, config={"window": 50})
    assert stats.n == 50
    assert stats.samples["k59"] == 2000


def test_record_finished_validates(tmp_path: Path) -> None:
    path = tmp_path / "spec_stats.json"
    with pytest.raises(ValueError):
        spec_stats.record_finished(path, "", 10)
    with pytest.raises(ValueError):
        spec_stats.record_finished(path, "k", -1)
    with pytest.raises(ValueError):
        spec_stats.record_finished(path, "k", True)  # type: ignore[arg-type]


def test_config_extraction_defaults() -> None:
    assert spec_stats.spec_stats_config_from_config({}) == {"window": 50, "min_samples": 5, "k": 3.0}
    cfg = spec_stats.spec_stats_config_from_config({"queue": {"spec_stats": {"window": 10, "k": 2, "junk": 1}}})
    assert cfg == {"window": 10, "min_samples": 5, "k": 2}
