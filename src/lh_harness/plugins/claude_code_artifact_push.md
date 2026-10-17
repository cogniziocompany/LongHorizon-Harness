---
# Reference slash-command contract for LHH-SESSION-CONTEXT-ARTIFACTS.
# Drop this file (or its equivalent) into the user's Claude Code
# ``.claude/commands/`` directory to install the slash command.
---

# /lh-artifact-push

Export the current Claude Code session as a LongHorizon-Harness ContextPacket
and print the assigned ``artifact_id`` for attachment to the next queue entry.

## Invocation

```
/lh-artifact-push [--title "..."] [--summary "..."] [--transcript path] \
                  [--plan path] [--files path] [--evidence path] \
                  [--print-only]
```

The slash-command runner sets ``CLAUDE_SESSION_ID`` and
``CLAUDE_SESSION_REF`` from the live session; the harness sets
``LH_HARNESS_BASE_URL`` and ``LH_HARNESS_WEB_TOKEN`` in the operator's shell
profile.

## Implementation

The command runs the reference Python script at:

```
src/lh_harness/plugins/claude_code_artifact_push.py
```

Either invoke the script directly via Python, or call the SDK:

```
lh-harness artifact push --packet /path/to/packet.json
```

The packet file is the output of ``build_packet()`` in the script; the
operator-facing CLI (``lh-harness artifact push``) accepts the same shape
and the same env-var contract, so a CI pipeline can use either path.

## Trust model

* Bearer is read from ``LH_HARNESS_WEB_TOKEN``; never echoed.
* The packet is redacted on write by the API's :func:`redact_value` pass.
* The render page (GET /artifacts/<id>) escapes HTML and sandboxes any
  HTML evidence -- script tags in the summary are not executed.
* Git is not required. ``files_touched`` may be empty; the producer does
  not need to be inside a repo.
