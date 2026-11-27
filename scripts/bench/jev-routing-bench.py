#!/usr/bin/env python3
"""JEV-LOCAL-ROUTING-EVAL benchmark — bench script (task JEV-LOCAL-ROUTING-EVAL).

Compares four decision surfaces on the same 50-item labelled decision set:

  1. ``tev1:4b`` on the ptait01 lane ``$LH_HARNESS_TEV1_4B_URL`` (Ollama /v1/systemone)
  2. ``tev1:0.8b`` on the ptait01 lane ``$LH_HARNESS_TEV1_08B_URL`` (Ollama /v1/systemone)
  3. ``ornith/qwen alias`` — the harness's current local model, served by Ollama on
     ``$OLLAMA_LOCAL_BASELINE_URL`` (``/api/generate`` with the prompt we construct).
  4. ``cloud`` — the harness's current cloud baseline (``$CLOUD_BASELINE_MODEL``,
     served via ``$ANTHROPIC_BASE_URL``/``/v1/messages``).

The bench is **READ-ONLY** in this PR. It builds the requests, optionally executes
them when ``--run`` is passed, and reports accuracy + median latency.

Usage::

    # Dry run — construct every request, print them, do not call any HTTP endpoint.
    PYTHONPATH=src python3 scripts/bench/jev-routing-bench.py --dry-run

    # Live run — actually call the lanes.  The operator MUST run on a fresh lane
    # or in a scratch container, never on CT110's working tree.
    LH_HARNESS_TEV1_4B_URL=http://192.168.21.110:11441 \
    LH_HARNESS_TEV1_08B_URL=http://192.168.21.110:11438 \
    OLLAMA_LOCAL_BASELINE_URL=http://192.168.21.110:11442 \
    ANTHROPIC_BASE_URL=https://litellm.easybutt0n.ai \
    CLOUD_BASELINE_MODEL=claude-haiku-4-5 \
        python3 scripts/bench/jev-routing-bench.py --run

    # Filter to one category for fast iteration.
    python3 scripts/bench/jev-routing-bench.py --run --only manager_next_step

Output::

    Per-category accuracy and median latency are written to ``--out`` (default
    ``scripts/bench/jev-routing-bench-results.json``) so the auditor can diff
    runs across commits.

Reference:
  docs/research/jev-local-routing-2026-10-17.md
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_DECISIONS_PATH = Path(__file__).resolve().parent / "jev-routing-decisions.jsonl"

# Categories and their gold-field names.
_CHOICE_GOLD = "gold"
_NOUL_GOLD_KEYS = ("complete", "clean", "aligned")  # for category auditor_complete_clean_aligned


def load_decisions(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}:{line_no}: bad JSON: {e}") from e
    return out


def build_tev1_request(item: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the Ollama /v1/systemone body for a single decision item.

    The item already has the correct question types and criteria; we just need
    to fill in the ``model`` and add ``keep_alive: 0`` so the lane does not
    hold residency after the request (the lanes also serve
    ornith-1.5:9b-256k at ~11.3 GB VRAM).
    """
    return {
        "model": model,
        "state": item["state"],
        "questions": item["questions"],
        "keep_alive": 0,
    }


def build_local_baseline_request(item: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the Ollama /api/generate body for the local-baseline (chat-style) run.

    The local baseline (``ornith/qwen alias``) does NOT speak /v1/systemone —
    it's a regular chat/generate model. We render the question as a small
    prompt with explicit answer-format instructions so the response is
    machine-parsable.
    """
    cats = item.get("questions", {})
    cat = item["category"]
    if cat in {"manager_next_step", "launcher_trio_choice", "provider_error_classify", "hard_signal_label"}:
        # Render as a forced-choice question.
        first_q = next(iter(cats.values()))
        criteria = first_q.get("criteria", {})
        criteria_lines = "\n".join(f"  - {k}: {v}" for k, v in criteria.items())
        prompt = (
            f"INSTRUCTION: Choose exactly one option from the list below. "
            f"Respond with ONLY the option key, no other text.\n\n"
            f"QUESTION ({first_q['type']}): {first_q['instructions']}\n\n"
            f"OPTIONS:\n{criteria_lines}\n\n"
            f"STATE:\n{item['state']}\n\n"
            f"ANSWER:"
        )
    elif cat == "auditor_complete_clean_aligned":
        names = list(cats.keys())
        prompt = (
            f"INSTRUCTION: For each of the {len(names)} boolean questions, "
            f"answer YES or NO, one per line, in order.\n\n"
            + "\n".join(
                f"Q{i+1}. {cats[n]['instructions']}" for i, n in enumerate(names)
            )
            + f"\n\nSTATE:\n{item['state']}\n\n"
            f"ANSWER (one YES/NO per line):"
        )
    elif cat == "queue_triage_score":
        first_q = next(iter(cats.values()))
        levels = first_q.get("criteria", [])
        prompt = (
            f"INSTRUCTION: Score the cleanliness 0..{len(levels)-1}. "
            f"Respond with a single integer in that range, no other text.\n\n"
            f"QUESTION ({first_q['type']}): {first_q['instructions']}\n\n"
            f"LEVELS (lowest to highest):\n"
            + "\n".join(f"  {i}: {lvl}" for i, lvl in enumerate(levels))
            + f"\n\nSTATE:\n{item['state']}\n\n"
            f"ANSWER:"
        )
    else:
        raise ValueError(f"unknown category: {cat}")

    return {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 64, "num_ctx": 2048},
    }


def build_cloud_request(item: dict[str, Any], model: str) -> dict[str, Any]:
    """Build the /v1/messages body for the cloud baseline (Anthropic-compatible).

    Same forced-choice / score framing as the local baseline. Uses
    max_tokens small because all answers are short.
    """
    cats = item.get("questions", {})
    cat = item["category"]
    if cat in {"manager_next_step", "launcher_trio_choice", "provider_error_classify", "hard_signal_label"}:
        first_q = next(iter(cats.values()))
        criteria = first_q.get("criteria", {})
        criteria_lines = "\n".join(f"  - {k}: {v}" for k, v in criteria.items())
        user = (
            f"Choose exactly one option from the list below. "
            f"Respond with ONLY the option key, no other text.\n\n"
            f"QUESTION ({first_q['type']}): {first_q['instructions']}\n\n"
            f"OPTIONS:\n{criteria_lines}\n\n"
            f"STATE:\n{item['state']}"
        )
    elif cat == "auditor_complete_clean_aligned":
        names = list(cats.keys())
        user = (
            f"For each of the {len(names)} boolean questions, answer YES or NO, "
            f"one per line, in order.\n\n"
            + "\n".join(
                f"Q{i+1}. {cats[n]['instructions']}" for i, n in enumerate(names)
            )
            + f"\n\nSTATE:\n{item['state']}"
        )
    elif cat == "queue_triage_score":
        first_q = next(iter(cats.values()))
        levels = first_q.get("criteria", [])
        user = (
            f"Score the cleanliness 0..{len(levels)-1}. Respond with a single "
            f"integer in that range, no other text.\n\n"
            f"QUESTION ({first_q['type']}): {first_q['instructions']}\n\n"
            f"LEVELS (lowest to highest):\n"
            + "\n".join(f"  {i}: {lvl}" for i, lvl in enumerate(levels))
            + f"\n\nSTATE:\n{item['state']}"
        )
    else:
        raise ValueError(f"unknown category: {cat}")

    return {
        "model": model,
        "max_tokens": 64,
        "temperature": 0.0,
        "messages": [{"role": "user", "content": user}],
    }


def _http_post_json(url: str, body: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — caller controls URL
        return json.loads(resp.read().decode("utf-8"))


def call_tev1(url: str, model: str, item: dict[str, Any]) -> tuple[dict[str, Any], float]:
    body = build_tev1_request(item, model)
    start = time.perf_counter()
    try:
        resp = _http_post_json(f"{url.rstrip('/')}/v1/systemone", body, timeout=10.0)
    except urllib.error.HTTPError as e:
        # surface error body for the auditor
        body_text = e.read().decode("utf-8", errors="replace")[:512]
        resp = {"_error": f"HTTP {e.code}: {body_text}"}
    elapsed = time.perf_counter() - start
    return resp, elapsed


def call_local_baseline(
    url: str, model: str, item: dict[str, Any]
) -> tuple[dict[str, Any], float]:
    body = build_local_baseline_request(item, model)
    start = time.perf_counter()
    try:
        resp = _http_post_json(f"{url.rstrip('/')}/api/generate", body, timeout=10.0)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:512]
        resp = {"_error": f"HTTP {e.code}: {body_text}"}
    elapsed = time.perf_counter() - start
    return resp, elapsed


def call_cloud(
    base_url: str,
    model: str,
    item: dict[str, Any],
    api_key: str | None,
) -> tuple[dict[str, Any], float]:
    body = build_cloud_request(item, model)
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/messages", data=data, method="POST", headers=headers
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:512]
        payload = {"_error": f"HTTP {e.code}: {body_text}"}
    elapsed = time.perf_counter() - start
    return payload, elapsed


# ---- answer extraction ----------------------------------------------------

def _tev1_answer_to_choice(answers: dict[str, Any]) -> str:
    """Take the first answer; return its `choice` key. Raise on error."""
    if not answers:
        raise ValueError("empty answers")
    first = next(iter(answers.values()))
    if "choice" not in first:
        raise ValueError(f"answer has no 'choice' key: {first}")
    return first["choice"]


def _tev1_answer_to_noul_map(answers: dict[str, Any]) -> dict[str, bool]:
    """Take all answers; map each named noul to a hard yes/no at threshold 0.5."""
    out: dict[str, bool] = {}
    for name, ans in answers.items():
        if "noul" not in ans:
            raise ValueError(f"answer {name} has no 'noul' key: {ans}")
        out[name] = float(ans["noul"]) > 0.5
    return out


def _tev1_answer_to_score(answers: dict[str, Any]) -> int:
    """Take the first answer; round its `score` (probability-weighted level)."""
    if not answers:
        raise ValueError("empty answers")
    first = next(iter(answers.values()))
    if "score" not in first:
        raise ValueError(f"answer has no 'score' key: {first}")
    return int(round(float(first["score"])))


def _text_response_to_choice(text: str, valid_keys: list[str]) -> str:
    """Best-effort: find the first valid option key in the model's text."""
    text = (text or "").strip()
    # Direct match.
    for k in valid_keys:
        if text == k:
            return k
    # Substring match — first valid key that appears.
    for k in valid_keys:
        if k in text:
            return k
    raise ValueError(f"no valid option key in text: {text!r}")


def _text_response_to_noul_map(text: str, names: list[str]) -> dict[str, bool]:
    """Parse 'YES/NO per line' response. Falls back to False if anything is wrong."""
    out: dict[str, bool] = {}
    lines = [ln.strip().upper() for ln in (text or "").splitlines() if ln.strip()]
    for i, name in enumerate(names):
        if i < len(lines):
            out[name] = lines[i].startswith("Y")
        else:
            out[name] = False
    return out


def _text_response_to_score(text: str, n_levels: int) -> int:
    """Parse single-int response."""
    text = (text or "").strip()
    for token in text.split():
        try:
            v = int(token)
        except ValueError:
            continue
        if 0 <= v < n_levels:
            return v
    raise ValueError(f"no valid score in text: {text!r}")


# ---- scoring --------------------------------------------------------------

def score_item(
    category: str,
    item: dict[str, Any],
    answer: Any,
    *,
    surface: str,
) -> tuple[bool, float]:
    """Return (correct, confidence). Confidence is tev1's `confidence` for choice/score,
    or `max(p, 1-p)` for noul. For text-only surfaces confidence is 1.0 because we
    already lost the calibration signal."""
    if surface == "tev1":
        answers = answer.get("answers") if isinstance(answer, dict) else None
        if not answers:
            return False, 0.0
        if category in {"manager_next_step", "launcher_trio_choice", "provider_error_classify", "hard_signal_label"}:
            pred = _tev1_answer_to_choice(answers)
            correct = pred == item["gold"]
            first = next(iter(answers.values()))
            confidence = float(first.get("confidence", 0.0))
            return correct, confidence
        if category == "auditor_complete_clean_aligned":
            pred = _tev1_answer_to_noul_map(answers)
            correct = pred == item["gold"]
            confidences = [
                max(float(a.get("noul", 0.5)), 1 - float(a.get("noul", 0.5)))
                for a in answers.values()
            ]
            return correct, statistics.mean(confidences) if confidences else 0.0
        if category == "queue_triage_score":
            pred = _tev1_answer_to_score(answers)
            correct = pred == int(item["gold"])
            first = next(iter(answers.values()))
            confidence = float(first.get("confidence", 0.0))
            return correct, confidence
        raise ValueError(f"unknown category: {category}")

    # text-only surfaces
    if surface in {"local", "cloud"}:
        if "_error" in (answer or {}):
            return False, 0.0
        # Pull the text from Ollama /api/generate or /v1/messages.
        if surface == "local":
            text = (answer or {}).get("response", "")
        else:
            # /v1/messages returns content as a list of blocks.
            try:
                blocks = (answer or {}).get("content", [])
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            except Exception:
                text = ""
        try:
            if category in {"manager_next_step", "launcher_trio_choice", "provider_error_classify", "hard_signal_label"}:
                first_q = next(iter(item["questions"].values()))
                valid_keys = list(first_q.get("criteria", {}).keys())
                pred = _text_response_to_choice(text, valid_keys)
                correct = pred == item["gold"]
                return correct, 1.0
            if category == "auditor_complete_clean_aligned":
                names = list(item["questions"].keys())
                pred = _text_response_to_noul_map(text, names)
                correct = pred == item["gold"]
                return correct, 1.0
            if category == "queue_triage_score":
                first_q = next(iter(item["questions"].values()))
                n_levels = len(first_q.get("criteria", []))
                pred = _text_response_to_score(text, n_levels)
                correct = pred == int(item["gold"])
                return correct, 1.0
        except ValueError:
            return False, 0.0
    raise ValueError(f"unknown surface: {surface}")


# ---- main loop ------------------------------------------------------------

def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def _build_results(decisions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-surface per-category result dict."""
    surfaces = ["tev1:4b", "tev1:0.8b", "local", "cloud"]
    out: dict[str, dict[str, Any]] = {}
    for s in surfaces:
        out[s] = {}
    # Per-category gold counts so accuracy is computable.
    for d in decisions:
        for s in surfaces:
            out[s].setdefault(
                d["category"],
                {"correct": 0, "total": 0, "latencies_ms": [], "confidences": []},
            )
    return out


def _print_request_dry(item: dict[str, Any]) -> None:
    """Print the four HTTP requests we'd send for one item, no network call."""
    print(f"--- {item['id']} ({item['category']}) ---")
    print("tev1:4b -> POST /v1/systemone")
    print(json.dumps(build_tev1_request(item, "tev1"), indent=2))
    print("tev1:0.8b -> POST /v1/systemone")
    print(json.dumps(build_tev1_request(item, "tev1:0.8b"), indent=2))
    print("local -> POST /api/generate")
    print(json.dumps(build_local_baseline_request(item, "ornith-1.5:9b-256k"), indent=2))
    print("cloud -> POST /v1/messages")
    print(json.dumps(build_cloud_request(item, "claude-haiku-4-5"), indent=2))
    print()


def run(args: argparse.Namespace) -> int:
    decisions = load_decisions(_DECISIONS_PATH)
    if args.only:
        decisions = [d for d in decisions if d["category"] == args.only]
        if not decisions:
            raise SystemExit(f"no decisions match --only {args.only!r}")

    if args.dry_run:
        for d in decisions:
            _print_request_dry(d)
        return 0

    # Resolve URLs from env.
    tev1_4b_url = _env("LH_HARNESS_TEV1_4B_URL") or "http://192.168.21.110:11441"
    tev1_08b_url = _env("LH_HARNESS_TEV1_08B_URL") or "http://192.168.21.110:11438"
    local_url = _env("OLLAMA_LOCAL_BASELINE_URL") or "http://192.168.21.110:11442"
    local_model = _env("OLLAMA_LOCAL_BASELINE_MODEL") or "qwen3.8:27b"
    cloud_base = _env("ANTHROPIC_BASE_URL") or "https://litellm.easybutt0n.ai"
    cloud_model = _env("CLOUD_BASELINE_MODEL") or "claude-haiku-4-5"
    cloud_key = _env("ANTHROPIC_API_KEY") or _env("LH_HARNESS_LITELLM_KEY")

    results = _build_results(decisions)

    for d in decisions:
        cat = d["category"]
        # tev1:4b
        resp4, dt4 = call_tev1(tev1_4b_url, "tev1", d)
        ok, conf = score_item(cat, d, resp4, surface="tev1")
        bucket = results["tev1:4b"][cat]
        bucket["correct"] += int(ok)
        bucket["total"] += 1
        bucket["latencies_ms"].append(dt4 * 1000)
        bucket["confidences"].append(conf)

        # tev1:0.8b
        resp08, dt08 = call_tev1(tev1_08b_url, "tev1:0.8b", d)
        ok, conf = score_item(cat, d, resp08, surface="tev1")
        bucket = results["tev1:0.8b"][cat]
        bucket["correct"] += int(ok)
        bucket["total"] += 1
        bucket["latencies_ms"].append(dt08 * 1000)
        bucket["confidences"].append(conf)

        # local baseline
        local_resp, dt_local = call_local_baseline(local_url, local_model, d)
        ok, conf = score_item(cat, d, local_resp, surface="local")
        bucket = results["local"][cat]
        bucket["correct"] += int(ok)
        bucket["total"] += 1
        bucket["latencies_ms"].append(dt_local * 1000)
        bucket["confidences"].append(conf)

        # cloud baseline
        cloud_resp, dt_cloud = call_cloud(cloud_base, cloud_model, d, cloud_key)
        ok, conf = score_item(cat, d, cloud_resp, surface="cloud")
        bucket = results["cloud"][cat]
        bucket["correct"] += int(ok)
        bucket["total"] += 1
        bucket["latencies_ms"].append(dt_cloud * 1000)
        bucket["confidences"].append(conf)

    # Summarise.
    summary: dict[str, Any] = {
        "n_decisions": len(decisions),
        "surfaces": {},
    }
    for surface, by_cat in sorted(results.items()):
        surface_acc = {"per_category": {}, "overall": {}}
        all_lat: list[float] = []
        all_correct = 0
        all_total = 0
        for cat, stats in sorted(by_cat.items()):
            acc = (stats["correct"] / stats["total"]) if stats["total"] else 0.0
            med = statistics.median(stats["latencies_ms"]) if stats["latencies_ms"] else 0.0
            avg_conf = (
                statistics.mean(stats["confidences"]) if stats["confidences"] else 0.0
            )
            surface_acc["per_category"][cat] = {
                "accuracy": round(acc, 4),
                "median_latency_ms": round(med, 1),
                "mean_confidence": round(avg_conf, 4),
                "n": stats["total"],
            }
            all_lat.extend(stats["latencies_ms"])
            all_correct += stats["correct"]
            all_total += stats["total"]
        surface_acc["overall"] = {
            "accuracy": round((all_correct / all_total) if all_total else 0.0, 4),
            "median_latency_ms": round(statistics.median(all_lat) if all_lat else 0.0, 1),
            "n": all_total,
        }
        summary["surfaces"][surface] = surface_acc

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {out_path}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Construct and print every request, do not call any HTTP endpoint.",
    )
    p.add_argument(
        "--run",
        action="store_true",
        help="Actually call the lanes. Operator must run on a fresh lane, never on CT110.",
    )
    p.add_argument(
        "--only",
        help="Filter to one category (e.g. manager_next_step).",
    )
    p.add_argument(
        "--out",
        default=str(_DECISIONS_PATH.parent / "jev-routing-bench-results.json"),
        help="Where to write the JSON summary when --run is set.",
    )
    args = p.parse_args()
    if not args.dry_run and not args.run:
        p.error("pass either --dry-run or --run")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())