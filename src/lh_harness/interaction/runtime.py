from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from ..adapters.claude_code import ClaudeCodeAdapter
from ..webapi.server import create_app
from .local import LocalInteractiveService


def build_interactive_service() -> LocalInteractiveService:
    root = os.environ.get("LH_HARNESS_INTERACTIVE_ROOT", "").strip()
    source_root = os.environ.get("LH_HARNESS_INTERACTIVE_SOURCE_ROOT", "").strip()
    if not root or not source_root:
        raise RuntimeError("LH_HARNESS_INTERACTIVE_ROOT and LH_HARNESS_INTERACTIVE_SOURCE_ROOT are required")
    root_path = Path(root).resolve()
    source_path = Path(source_root).resolve()
    prompt_dir = root_path / "prompts"
    model = os.environ.get("LH_HARNESS_INTERACTIVE_MODEL") or None
    base_url = os.environ.get("ANTHROPIC_BASE_URL") or None
    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or None
    roles = {
        "manager": "manager",
        "executor": "cli_executor",
        "auditor": "cli_auditor",
        "final_response": "final_response",
    }

    def factory(role: str) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(
            model=model,
            api_key=api_key,
            base_url=base_url,
            workspace_path=str(source_path),
            prompt_dir=str(prompt_dir),
            role=roles[role],
        )

    return LocalInteractiveService(root_path, source_root=source_path, adapter_factory=factory)


def main() -> None:
    service = build_interactive_service()
    root = Path(os.environ["LH_HARNESS_INTERACTIVE_ROOT"]).resolve()
    host = os.environ.get("LH_HARNESS_INTERACTIVE_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Interactive Office Harness must bind to loopback")
    uvicorn.run(
        create_app(
            runs_root=root / "api",
            workspace_root=root / "workspaces",
            interactive_service=service,
            auth_token=os.environ.get("LH_HARNESS_WEB_TOKEN"),
            bind_host=host,
        ),
        host=host,
        port=int(os.environ.get("LH_HARNESS_INTERACTIVE_PORT", "18799")),
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
