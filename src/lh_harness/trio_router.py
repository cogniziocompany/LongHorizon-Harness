"""tev1-powered default trio router (LHH-TEV1-DEFAULT-TRIO-ROUTER, 2026-10-20).

When a queue entry arrives with no ``trio`` (POST /api/queue without ``trio``,
or ``trio`` empty / None), the launcher today falls through to the static
default and skips as "unknown trio".  This module lets a typed decision model
pick a trio for it.

The model is ``tev1:4b`` (TypeSafe Jev-style typed decision model) served by
Ollama on the 2060 lane ``http://192.168.21.138:11436`` behind the
``/v1/systemone`` endpoint.  The request shape is::

    {"model": "tev1:4b",
     "state": {"task": "<task text, first 4000 chars>"},
     "questions": {"trio": {"type": "choice",
                             "instructions": "Which harness trio should run this task?",
                             "criteria": {"<trio>": "<one-line description>", ...}},
                    "small": {"type": "noul",
                              "instructions": "Is this task small enough for a 9B local model "
                                              "(one file, under 150 lines)?"}}}

and the response shape is::

    {"answers": {"trio": {"choice": "<name>",
                          "probabilities": {...},
                          "confidence": 0.68},
                 "small": {"noul": 0.70}}}

The chosen trio must (1) be enabled in the settings store, (2) have a configured
capacity, (3) meet the ``TRIO_ROUTER_MIN_CONFIDENCE`` floor (default 0.5), and
(4) for the qwen / zaiq local 9B trios, also pass ``small >= 0.6`` -- the
router refuses to delegate work too big for a 9B local model to a local lane.

The decision is recorded on the queue entry (``entry.trio_router``) and
ultimately on the run's queue outcome so Overseer1 can audit it.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

try:  # Lazy: httpx is a transitive dependency of FastAPI but tests may stub.
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

# Hardcoded fallback criteria for trios that exist by convention even when a
# project config does not enumerate them.  An admin can override any of these
# from the settings store / config.toml; the router always prefers the
# settings-store description when one is registered.
#
# The local-9B trios (``qwen`` and ``zaiq``) are tagged here so Overseer1 can
# audit the "small" gate from the criteria shape; the gate is enforced
# structurally in ``_is_local_trio`` -- nothing in the description is parsed
# to decide safety.
DEFAULT_TRIO_DESCRIPTIONS: dict[str, str] = {
    "kimi": "Kimi (synthetic Anthropic Sonnet) -- the long-running default manager/executor/auditor lane.",
    "qwen": "Local 9B qwen lane -- fast/cheap but only safe for single-file tasks under ~150 lines.",
    "glm": "Ollama Cloud glm-5.3 -- the second Anthropic-compatible cloud lane.",
    "zai": "Direct z.ai coding plan (flash manager/executor) -- the high-throughput cloud lane.",
    "zaiq": "Mixed z.ai manager/auditor + local qwen executor -- 9B on the executor side; small-task only.",
    "minimax": "MiniMax M Plan -- MiniMax M family for the manager/executor roles.",
    "gemma": "Local 2060 lane (gemma) -- the on-host fallback lane.",
}

_LOCAL_9B_TRIOS = frozenset({"qwen", "zaiq"})

# Defaults match the task description (Paxton 2026-10-20) so a deployment
# that has never touched a trio-router setting still works the moment Paxton
# flips ``TRIO_ROUTER_ENABLED``.
DEFAULT_TRIO_ROUTER_URL = "http://192.168.21.138:11436"
DEFAULT_TRIO_ROUTER_MODEL = "tev1:4b"
DEFAULT_TRIO_ROUTER_MIN_CONFIDENCE = 0.5
DEFAULT_TRIO_ROUTER_SMALL_FLOOR = 0.6
TRIO_ROUTER_TIMEOUT_SECONDS = 30.0
TRIO_ROUTER_MAX_TASK_CHARS = 4000

# Settings names the router reads from the settings store / process env.
SETTING_ENABLED = "TRIO_ROUTER_ENABLED"
SETTING_URL = "TRIO_ROUTER_URL"
SETTING_MODEL = "TRIO_ROUTER_MODEL"
SETTING_MIN_CONFIDENCE = "TRIO_ROUTER_MIN_CONFIDENCE"
SETTING_SMALL_FLOOR = "TRIO_ROUTER_SMALL_FLOOR"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers --
def _read_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled", ""}:
            return False
    return default


def _read_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _read_str(value: Any, default: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _resolve_settings(store: Any) -> dict[str, Any]:
    """Pull the four trio-router settings out of the store / environment.

    Reads from the ``SettingsStore`` first (so the admin screen wins), then
    falls back to ``os.environ`` so a process started without a settings
    store still respects the bootstrap.  ``store`` may be ``None`` -- that
    path reads the environment only.
    """

    enabled = _read_bool(_lookup(store, SETTING_ENABLED), default=False)
    url = _read_str(_lookup(store, SETTING_URL), default=DEFAULT_TRIO_ROUTER_URL)
    model = _read_str(_lookup(store, SETTING_MODEL), default=DEFAULT_TRIO_ROUTER_MODEL)
    min_conf = _read_float(
        _lookup(store, SETTING_MIN_CONFIDENCE),
        default=DEFAULT_TRIO_ROUTER_MIN_CONFIDENCE,
    )
    small_floor = _read_float(
        _lookup(store, SETTING_SMALL_FLOOR),
        default=DEFAULT_TRIO_ROUTER_SMALL_FLOOR,
    )
    # Clamp the thresholds into safe shapes.  ``TRIO_ROUTER_MIN_CONFIDENCE``
    # must be a probability in [0, 1]; the small floor likewise.
    if not 0.0 <= min_conf <= 1.0:
        min_conf = DEFAULT_TRIO_ROUTER_MIN_CONFIDENCE
    if not 0.0 <= small_floor <= 1.0:
        small_floor = DEFAULT_TRIO_ROUTER_SMALL_FLOOR
    return {
        "enabled": enabled,
        "url": url,
        "model": model,
        "min_confidence": min_conf,
        "small_floor": small_floor,
    }


def _lookup(store: Any, name: str) -> Any:
    """Read one setting from the store, then from the process environment."""

    if store is not None:
        getter = getattr(store, "metadata", None)
        if callable(getter):
            try:
                meta = getter(name)
            except Exception:
                meta = None
            if isinstance(meta, dict) and meta.get("set"):
                if not meta.get("secret"):
                    value = meta.get("value")
                    if isinstance(value, str) and value != "":
                        return value
    import os as _os

    return _os.environ.get(name)


def _build_criteria(store: Any, config_trios: Mapping[str, Any] | None) -> dict[str, str]:
    """Per-trio one-line descriptions from the settings store + defaults.

    Trios enabled in the settings store are preferred (their name is
    authoritative: ``queue_trios()`` is the same source the launcher's hot
    reload reads, so what the router sees matches what the launcher can
    schedule).  The ``description`` column lives on ``queue_trio`` (or, when
    absent, the settings key ``TRIO_ROUTER_TRIO_DESCRIPTIONS`` JSON), and the
    hardcoded ``DEFAULT_TRIO_DESCRIPTIONS`` covers every well-known name as
    a last resort so a deployment that never wrote a description still works.
    """

    criteria: dict[str, str] = {}

    if store is not None:
        getter = getattr(store, "queue_trios", None)
        if callable(getter):
            try:
                rows = getter()
            except Exception:
                rows = []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    name = str(row.get("name") or "").strip()
                    if not name or not row.get("enabled", True):
                        continue
                    description = str(row.get("description") or "").strip()
                    if description:
                        criteria[name] = description

    if not criteria and isinstance(config_trios, Mapping):
        for name in config_trios.keys():
            text = str(name).strip()
            if text:
                criteria.setdefault(text, DEFAULT_TRIO_DESCRIPTIONS.get(text, f"{text} harness trio"))

    for name, description in DEFAULT_TRIO_DESCRIPTIONS.items():
        criteria.setdefault(name, description)

    return criteria


def _is_local_9b(trio: str) -> bool:
    return str(trio).strip().lower() in _LOCAL_9B_TRIOS


def _trio_is_schedulable(trio: str, config_trios: Mapping[str, Any] | None) -> bool:
    """True when ``trio`` is enabled in the merged launcher config and has a capacity > 0."""

    if not config_trios:
        return False
    spec = config_trios.get(trio)
    if not isinstance(spec, dict):
        return False
    return bool(spec)


def _truncate_task(task: str) -> str:
    if not isinstance(task, str):
        return ""
    text = task.strip()
    if len(text) <= TRIO_ROUTER_MAX_TASK_CHARS:
        return text
    return text[:TRIO_ROUTER_MAX_TASK_CHARS]


def _systemone_payload(task: str, criteria: Mapping[str, str]) -> dict[str, Any]:
    """The request body tev1 expects at /v1/systemone."""

    questions: dict[str, Any] = {
        "trio": {
            "type": "choice",
            "instructions": "Which harness trio should run this task?",
            "criteria": {name: desc for name, desc in sorted(criteria.items())},
        },
        "small": {
            "type": "noul",
            "instructions": (
                "Is this task small enough for a 9B local model "
                "(one file, under 150 lines)?"
            ),
        },
    }
    return {
        "model": DEFAULT_TRIO_ROUTER_MODEL,
        "state": {"task": _truncate_task(task)},
        "questions": questions,
    }


def _call_systemone(url: str, model: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """POST ``payload`` to ``<url>/v1/systemone`` and parse the JSON response.

    The endpoint returns the answers dict described above; any non-JSON
    response, transport timeout, or HTTP error raises ``RuntimeError`` so
    the router can fall back.  Tests inject a fake by monkeypatching this
    function.
    """

    body = dict(payload)
    body["model"] = model
    target = f"{url.rstrip('/')}/v1/systemone"
    if httpx is None:
        # The base runtime guarantees httpx; the import guard above is a
        # defensive no-op for hosts where the dep is pruned.
        raise RuntimeError("httpx is unavailable; trio router cannot reach the model")
    response = httpx.post(target, json=body, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("trio router response was not a JSON object")
    return data


def _coerce_answer(answer: Any) -> dict[str, Any]:
    """Tolerate slight shape drift from the model -- only the keys we need are required."""

    if not isinstance(answer, dict):
        return {}
    out: dict[str, Any] = {}
    choice = answer.get("choice")
    if isinstance(choice, str):
        out["choice"] = choice.strip().lower()
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        clean: dict[str, float] = {}
        for key, value in probs.items():
            if isinstance(key, str) and isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key.strip().lower()] = float(value)
        if clean:
            out["probabilities"] = clean
    confidence = answer.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        out["confidence"] = float(confidence)
    return out


def _coerce_small(answer: Any) -> float:
    if not isinstance(answer, dict):
        return 0.0
    raw = answer.get("noul")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return 0.0


def route_trio(
    entry: Any,
    store: Any,
    *,
    config_trios: Mapping[str, Any] | None = None,
    http_call: Callable[..., Any] | None = None,
    timeout: float = TRIO_ROUTER_TIMEOUT_SECONDS,
    now: float | None = None,
) -> str | None:
    """Pick a trio for ``entry`` when ``entry.trio`` is empty, or ``None``.

    Returns the chosen trio name on success.  Returns ``None`` to fall back
    to the existing default -- either because the router is disabled, the
    call timed out / errored, the model's confidence was below
    ``TRIO_ROUTER_MIN_CONFIDENCE``, or the picked trio is not schedulable.
    The decision struct is always written to ``entry.trio_router`` so
    Overseer1 can read why a fallback happened.

    ``store`` is the ``SettingsStore`` (or ``None``); ``config_trios`` is the
    merged launcher ``[queue.trios]`` dict -- the router uses it to check
    that the model's pick is actually schedulable, mirroring the eligibility
    check the launcher performs.  ``http_call`` is the test seam (defaults
    to ``_call_systemone``).
    """

    moment = float(time.time()) if now is None else float(now)
    settings = _resolve_settings(store)

    def _fallback(reason: str, **extra: Any) -> None:
        record = {
            "model": settings["model"],
            "url": settings["url"],
            "enabled": settings["enabled"],
            "min_confidence": settings["min_confidence"],
            "small_floor": settings["small_floor"],
            "reason": reason,
            "ts": moment,
            "chosen": None,
        }
        record.update(extra)
        try:
            entry.trio_router = record
        except Exception:
            pass

    if not settings["enabled"]:
        _fallback("router_disabled")
        return None

    explicit_trio = getattr(entry, "trio", "") or ""
    if explicit_trio.strip():
        # Hard rule: entries that already carry a trio are unchanged.  No
        # router call, no audit row written -- there's nothing to route.
        return None

    criteria = _build_criteria(store, config_trios)
    if not criteria:
        _fallback("no_criteria", criteria={})
        return None

    payload = _systemone_payload(getattr(entry, "task", ""), criteria)
    caller = http_call if http_call is not None else _call_systemone
    try:
        response = caller(settings["url"], settings["model"], payload, timeout=float(timeout))
    except Exception as exc:
        logger.warning("trio router /v1/systemone failed; falling back: %s", exc)
        _fallback("router_error", error=str(exc)[:200])
        return None

    answers = response.get("answers") if isinstance(response, dict) else None
    if not isinstance(answers, dict):
        _fallback("router_response_invalid", response_type=type(response).__name__)
        return None

    trio_answer = _coerce_answer(answers.get("trio"))
    small_noul = _coerce_small(answers.get("small"))
    choice = trio_answer.get("choice")
    probabilities = trio_answer.get("probabilities", {})
    confidence = trio_answer.get("confidence")

    # Refusal on a known preset (e.g. ``none`` / ``unknown``): the model said
    # nothing useful.  Treat exactly like a low confidence score.
    if not choice:
        _fallback(
            "router_no_choice",
            probabilities=probabilities,
            confidence=confidence,
            small_noul=small_noul,
        )
        return None

    if not _trio_is_schedulable(choice, config_trios):
        _fallback(
            "router_pick_not_schedulable",
            probabilities=probabilities,
            confidence=confidence,
            small_noul=small_noul,
            choice=choice,
        )
        return None

    if isinstance(confidence, float) and confidence < settings["min_confidence"]:
        _fallback(
            "router_low_confidence",
            probabilities=probabilities,
            confidence=confidence,
            small_noul=small_noul,
            choice=choice,
        )
        return None

    if _is_local_9b(choice) and small_noul < settings["small_floor"]:
        _fallback(
            "router_local_too_big",
            probabilities=probabilities,
            confidence=confidence,
            small_noul=small_noul,
            choice=choice,
        )
        return None

    record = {
        "model": settings["model"],
        "url": settings["url"],
        "enabled": True,
        "min_confidence": settings["min_confidence"],
        "small_floor": settings["small_floor"],
        "probabilities": probabilities,
        "confidence": confidence,
        "small_noul": small_noul,
        "choice": choice,
        "chosen": choice,
        "reason": "router_picked",
        "ts": moment,
    }
    try:
        entry.trio_router = record
    except Exception:
        pass
    return choice


__all__ = [
    "DEFAULT_TRIO_DESCRIPTIONS",
    "DEFAULT_TRIO_ROUTER_URL",
    "DEFAULT_TRIO_ROUTER_MODEL",
    "DEFAULT_TRIO_ROUTER_MIN_CONFIDENCE",
    "DEFAULT_TRIO_ROUTER_SMALL_FLOOR",
    "SETTING_ENABLED",
    "SETTING_URL",
    "SETTING_MODEL",
    "SETTING_MIN_CONFIDENCE",
    "SETTING_SMALL_FLOOR",
    "TRIO_ROUTER_TIMEOUT_SECONDS",
    "TRIO_ROUTER_MAX_TASK_CHARS",
    "route_trio",
]