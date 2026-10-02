#!/usr/bin/env python3
"""Browser telemetry E2E for the Web workbench.

Opens the workbench the way an operator does (deep links included) and
records what the browser saw: uncaught page errors, console errors, HTTP
failures, WebSocket handshakes/close codes and per-endpoint latency.  It then
asserts the things that made the UI feel flaky:

* a deep link renders the run instead of crashing the React tree;
* a missing/expired credential produces one prompt, not a request storm;
* the live stream connects and delivers frames.

Usage (token is read from the environment, never from argv):

    LH_HARNESS_WEB_TOKEN=... python e2e/web_telemetry.py \
        --base-url http://192.168.21.168:8799 --channel msedge

Behind an SSO edge that injects the bearer, omit the token and pass
``--storage-state`` with a signed-in browser state instead.

Requires ``pip install playwright``.  ``--channel msedge``/``chrome`` uses the
installed browser, so no Playwright browser download is needed.
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
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

WEB_TOKEN_KEY = "lh-web-token"


@dataclass
class Telemetry:
    """Everything one scenario's browser page reported."""

    name: str
    url: str
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    http_failures: list[dict[str, Any]] = field(default_factory=list)
    request_failures: list[str] = field(default_factory=list)
    api_ms: dict[str, list[float]] = field(default_factory=dict)
    ws_opened: int = 0
    ws_frames: int = 0
    ws_closed: int = 0
    final_path: str = ""
    root_children: int = 0
    body_text_chars: int = 0
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append((label, bool(ok), detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def status_count(self, status: int) -> int:
        return sum(1 for item in self.http_failures if item["status"] == status)

    def to_dict(self) -> dict[str, Any]:
        latency = {
            path: {
                "n": len(values),
                "p50_ms": round(statistics.median(values), 1),
                "max_ms": round(max(values), 1),
            }
            for path, values in sorted(self.api_ms.items())
        }
        return {
            "scenario": self.name,
            "url": self.url,
            "passed": self.passed,
            "checks": [{"check": label, "ok": ok, "detail": detail} for label, ok, detail in self.checks],
            "page_errors": self.page_errors,
            "console_errors": self.console_errors[:20],
            "http_failures": self.http_failures[:40],
            "http_failure_count": len(self.http_failures),
            "request_failures": self.request_failures[:20],
            "websocket": {"opened": self.ws_opened, "frames": self.ws_frames, "closed": self.ws_closed},
            "api_latency": latency,
            "final_path": self.final_path,
            "root_children": self.root_children,
            "body_text_chars": self.body_text_chars,
        }


def _endpoint_label(url: str, run_id: str) -> str:
    parts = urlsplit(url)
    path = parts.path.replace(quote(run_id, safe=""), "{run}") if run_id else parts.path
    if parts.query.startswith("fields=summary"):
        path += "?fields=summary"
    return path


def _discover_run_id(base_url: str, token: str) -> str:
    request = urllib.request.Request(f"{base_url}/api/runs?fields=summary")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            runs = json.load(response).get("runs") or []
    except (urllib.error.URLError, ValueError) as exc:
        raise SystemExit(f"could not list runs to pick a deep-link target: {exc}") from exc
    if not runs:
        raise SystemExit("server has no runs; pass --run-id")
    running = [run for run in runs if str(run.get("status")) == "running"]
    return str((running or runs)[0]["id"])


def _observe(page: Any, telemetry: Telemetry, base_url: str, run_id: str) -> None:
    started: dict[Any, float] = {}

    def on_request(request: Any) -> None:
        started[request] = time.monotonic()

    def on_response(response: Any) -> None:
        url = response.url
        if not url.startswith(base_url):
            return
        label = _endpoint_label(url, run_id)
        begun = started.pop(response.request, None)
        if begun is not None and urlsplit(url).path.startswith("/api/"):
            telemetry.api_ms.setdefault(label, []).append((time.monotonic() - begun) * 1000)
        if response.status >= 400:
            telemetry.http_failures.append({"status": response.status, "endpoint": label})

    def on_websocket(websocket: Any) -> None:
        telemetry.ws_opened += 1
        websocket.on("framereceived", lambda _payload: setattr(telemetry, "ws_frames", telemetry.ws_frames + 1))
        websocket.on("close", lambda _ws: setattr(telemetry, "ws_closed", telemetry.ws_closed + 1))

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", lambda request: telemetry.request_failures.append(
        f"{request.method} {_endpoint_label(request.url, run_id)}: {request.failure}"
    ))
    page.on("pageerror", lambda error: telemetry.page_errors.append(str(error)))
    page.on("console", lambda message: telemetry.console_errors.append(message.text[:300])
            if message.type == "error" else None)
    page.on("websocket", on_websocket)


def _run_scenario(
    browser: Any,
    *,
    name: str,
    base_url: str,
    path: str,
    run_id: str,
    token: str,
    settle: float,
    storage_state: str | None,
) -> Telemetry:
    telemetry = Telemetry(name=name, url=f"{base_url}{path}")
    context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
    try:
        if token:
            # sessionStorage is per tab, so this models "the operator already
            # entered the token in this tab"; a context without it models a
            # deep link opened in a fresh tab.
            context.add_init_script(
                f"window.sessionStorage.setItem({json.dumps(WEB_TOKEN_KEY)}, {json.dumps(token)});"
            )
        page = context.new_page()
        _observe(page, telemetry, base_url, run_id)
        page.goto(telemetry.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(int(settle * 1000))
        telemetry.final_path = urlsplit(page.url).path
        telemetry.root_children = page.evaluate("document.getElementById('root')?.childElementCount ?? 0")
        telemetry.body_text_chars = page.evaluate("document.body.innerText.trim().length")
    finally:
        context.close()
    return telemetry


def _common_checks(telemetry: Telemetry) -> None:
    telemetry.check("no uncaught page errors", not telemetry.page_errors, "; ".join(telemetry.page_errors)[:300])
    telemetry.check(
        "app shell stays mounted",
        telemetry.root_children > 0 and telemetry.body_text_chars > 0,
        f"root children={telemetry.root_children}, text chars={telemetry.body_text_chars}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("LH_WEB_URL", "http://127.0.0.1:8799"))
    parser.add_argument("--run-id", default="", help="Deep-link target (default: newest running run).")
    parser.add_argument("--token-env", default="LH_HARNESS_WEB_TOKEN", help="Env var holding the bearer token.")
    parser.add_argument("--channel", default=os.environ.get("LH_E2E_BROWSER_CHANNEL", ""),
                        help="Installed browser channel (msedge, chrome); default is Playwright's chromium.")
    parser.add_argument("--storage-state", default=None, help="Playwright storage state of a signed-in SSO session.")
    parser.add_argument("--settle", type=float, default=15.0, help="Seconds to observe each scenario.")
    parser.add_argument("--max-unauth-401", type=int, default=8,
                        help="401 budget for an unauthenticated deep link over the settle window.")
    parser.add_argument("--json", dest="json_path", default="", help="Write the full telemetry report here.")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed: pip install playwright", file=sys.stderr)
        return 2

    base_url = args.base_url.rstrip("/")
    token = os.environ.get(args.token_env, "").strip()
    run_id = args.run_id or _discover_run_id(base_url, token)
    run_path = f"/runs/{quote(run_id, safe='')}"
    results: list[Telemetry] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel or None, headless=True)
        try:
            shared = {"base_url": base_url, "run_id": run_id, "settle": args.settle,
                      "storage_state": args.storage_state}

            if token:
                cold = _run_scenario(browser, name="deep_link_fresh_tab_no_token", path=run_path, token="", **shared)
                _common_checks(cold)
                unauthorized = cold.status_count(401)
                cold.check(
                    "missing credential does not cause a request storm",
                    unauthorized <= args.max_unauth_401,
                    f"{unauthorized} x 401 in {args.settle:.0f}s (budget {args.max_unauth_401})",
                )
                cold.check(
                    "stream does not reconnect-loop without a credential",
                    cold.ws_opened <= 2,
                    f"{cold.ws_opened} WebSocket handshakes",
                )
                cold.check("deep link path is preserved", cold.final_path == run_path, cold.final_path)
                results.append(cold)

            for name, path in (
                ("deep_link_authenticated", run_path),
                ("gate_deep_link_authenticated", f"{run_path}/gates/telemetry-probe"),
                ("root_authenticated", "/"),
            ):
                telemetry = _run_scenario(browser, name=name, path=path, token=token, **shared)
                _common_checks(telemetry)
                telemetry.check(
                    "no HTTP failures",
                    not telemetry.http_failures,
                    ", ".join(f"{item['status']} {item['endpoint']}" for item in telemetry.http_failures[:6]),
                )
                telemetry.check(
                    "live stream connected and delivered frames",
                    telemetry.ws_opened >= 1 and telemetry.ws_frames >= 1,
                    f"handshakes={telemetry.ws_opened}, frames={telemetry.ws_frames}",
                )
                telemetry.check(
                    "stream is stable (no reconnect churn)",
                    telemetry.ws_opened <= 2,
                    f"{telemetry.ws_opened} WebSocket handshakes",
                )
                if path != "/":
                    # A gate link keeps its /gates/<id> segment so it can be copied.
                    telemetry.check("deep link opens the linked run", telemetry.final_path == path,
                                    telemetry.final_path)
                else:
                    telemetry.check("root lands on a run URL", telemetry.final_path.startswith("/runs/"),
                                    telemetry.final_path)
                results.append(telemetry)
        finally:
            browser.close()

    report = {"base_url": base_url, "run_id": run_id, "scenarios": [item.to_dict() for item in results]}
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)

    for item in results:
        print(f"\n[{'PASS' if item.passed else 'FAIL'}] {item.name}  ({item.url})")
        for label, ok, detail in item.checks:
            print(f"   {'ok  ' if ok else 'FAIL'} {label}" + (f" -- {detail}" if detail else ""))
        for path, values in sorted(item.api_ms.items()):
            print(f"        {path}: n={len(values)} p50={statistics.median(values):.0f}ms max={max(values):.0f}ms")
    failed = [item.name for item in results if not item.passed]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
