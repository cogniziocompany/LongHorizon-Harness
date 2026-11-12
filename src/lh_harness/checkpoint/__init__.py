"""V1.5 T4.1: trusted checkpoint and delta manifests (``trusted-handoff/v2``).

This package owns the *manifest* half of the V1.5 checkpoint/delta plane:

1. ``manifest.py`` -- ``canonical_bytes`` (``canonical-json/v1``),
   ``document_digest`` (the document's own ``digest`` key excluded),
   ``CheckpointManifest`` (the base) and ``DeltaManifest`` (predecessor
   digest plus contiguous sequence), the pinned ``TrustedHandoff``, and the
   validation library that checks a baseline and then every delta against
   that handoff.

Scope boundary: this is T4.1 only.  Restoration orchestration (T4.3 --
turning a validated chain into a restored context for a destination scope)
and durable acknowledgement packets (T4.4) are deliberately absent.  The
validation here never materializes or mutates restored state; it proves
that the manifest chain is complete and trusted, which is the prerequisite
T4.3 consumes.

Source of record: ``cognizioware`` ``design/factory-specs/v1.5`` at
``f4d8b2a9c7f4fc37b532d921ad956b33e937862b`` --
``06-solution-snippets.md`` section 5, ``02-code-structure.md``
(``CheckpointService``), and ``03-task-breakdown.md`` T4.1.  The trusted
requirements (tenant/task/policy revision, mandatory value schemas and the
trusted base/terminal identities, sequences and content digests) come from
the authenticated handoff and never from the checkpoint itself.
"""

from __future__ import annotations

from .manifest import (
    CANONICAL_JSON_VERSION,
    SCHEMA_ID,
    ChainSummary,
    CheckpointManifest,
    DeltaManifest,
    IncompleteContext,
    TrustedHandoff,
    canonical_bytes,
    document_digest,
    validate_chain,
    validate_checkpoint_manifest,
    validate_delta_manifest,
)

__all__ = [
    "CANONICAL_JSON_VERSION",
    "SCHEMA_ID",
    "ChainSummary",
    "CheckpointManifest",
    "DeltaManifest",
    "IncompleteContext",
    "TrustedHandoff",
    "canonical_bytes",
    "document_digest",
    "validate_chain",
    "validate_checkpoint_manifest",
    "validate_delta_manifest",
]
