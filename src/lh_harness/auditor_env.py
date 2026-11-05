"""The auditor subprocess environment, built FROM SCRATCH.

The auditor is a read-only GitHub verifier.  It runs an agent CLI, and that
agent CLI may shell out to ``gh``.  Whatever credentials the auditor's
process inherits are credentials the auditor can read -- and, before this
module existed, an inherited ``gh`` login (a ``hosts.yml`` under
``$HOME/.config/gh``) or an executor ``GH_TOKEN`` was enough to turn a
``gh auth token`` or ``gh config get oauth_token`` into a write-capable
credential printed into the auditor's context.

So the auditor's child environment is not "the parent environment, minus a
few names".  It is BUILT FROM SCRATCH:

*   a minimal, explicit base (PATH, HOME, locale, terminal settings);
*   NO ``GH_TOKEN``, ``GITHUB_TOKEN``, ``GH_ENTERPRISE_TOKEN``,
    ``GITHUB_ENTERPRISE_TOKEN``, no fleet/admin token variable, and no
    ``gh`` configuration directory inherited from the harness user;
*   ``GH_CONFIG_DIR`` pointing at a FRESH EMPTY temp dir, so ``gh`` has no
    stored credential to fall back on; and
*   ``GH_TOKEN`` set ONLY from :data:`AUDITOR_GITHUB_TOKEN_ENV`, and only
    when that variable resolves to a token that is not a shared credential.

The token VALUE never appears in this module's exceptions, in any log, or in
any representation of the returned mapping beyond the mapping itself.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping

from .auditor_verify import (
    SHARED_CREDENTIAL_ENV_VARS,
    resolve_auditor_credential,
)

__all__ = [
    "AUDITOR_CREDENTIAL_ENV_NAMES",
    "build_auditor_subprocess_env",
    "auditor_gh_config_dir",
]

#: Every environment variable that can carry a GitHub credential -- or point
#: ``gh`` at a host the auditor was not meant to talk to -- into a child
#: process.  These are dropped from the auditor's environment
#: unconditionally: the auditor's own token is re-introduced separately,
#: under ``GH_TOKEN``, and only when it resolves to the auditor's OWN
#: fine-grained token.
AUDITOR_CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GH_ENTERPRISE_HOST",
    "GH_HOST",
    *SHARED_CREDENTIAL_ENV_VARS,
)

#: The environment variables the auditor's child starts from.  Everything here
#: is inert: no credential, no host, no configuration directory.
_AUDITOR_BASE_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "SHELL",
    "TERM",
)

#: Variables that make ``git`` fail fast instead of prompting for a
#: credential.  The auditor never writes, and never asks.
_AUDITOR_BASE_OVERRIDES: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "/bin/false",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
}


def auditor_gh_config_dir(config_dir: str | Path | None = None) -> Path:
    """Return the FRESH EMPTY directory ``gh`` uses for its configuration.

    ``gh`` reads its hosts file and its stored tokens from
    ``$GH_CONFIG_DIR`` (or ``$HOME/.config/gh``).  Pointing it at a directory
    that exists and is empty means ``gh`` finds no stored credential, so it
    can only authenticate with the ``GH_TOKEN`` in the environment -- the
    auditor's own read-only token, and nothing else.
    """

    if config_dir is None:
        config_dir = tempfile.mkdtemp(prefix="lh-auditor-gh-config-")
    path = Path(config_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_auditor_subprocess_env(
    source: Mapping[str, str] | None = None,
    *,
    config_dir: str | Path | None = None,
) -> dict[str, str]:
    """Build the auditor's child environment from scratch.

    The returned mapping is the COMPLETE environment of the auditor's
    subprocess: it is not derived by deleting keys from the parent's
    environment, so a credential the harness does not know about cannot leak
    through it by omission.

    ``source`` defaults to ``os.environ``.  It is read for the variables named
    in :data:`_AUDITOR_BASE_ENV`, and for the auditor's own credential
    (which :func:`lh_harness.auditor_verify.resolve_auditor_credential`
    resolves from :data:`AUDITOR_GITHUB_TOKEN_ENV`, checking it against the
    shared credentials in the same mapping).  No other variable in
    ``source`` is carried over: every credential-named variable in it is
    ignored, and every variable the harness does not know about is ignored
    too.
    """

    env_source = os.environ if source is None else source

    env: dict[str, str] = {}
    for name in _AUDITOR_BASE_ENV:
        value = env_source.get(name)
        if value:
            env[name] = value
    env.update(_AUDITOR_BASE_OVERRIDES)

    # A fresh, empty gh configuration directory: no hosts.yml, no stored
    # token, no fallback to the harness user's login.
    gh_config_dir = auditor_gh_config_dir(config_dir)
    env["GH_CONFIG_DIR"] = str(gh_config_dir)

    # The auditor's OWN token, and only that, becomes the child's GH_TOKEN.
    # ``resolve_auditor_credential`` refuses a shared/fleet credential and
    # never returns a value in its repr or in the exception message.
    credential = resolve_auditor_credential(env_source)

    # Belt and braces: no credential-named variable may survive from the
    # source mapping, whatever it happened to carry.  The auditor's own
    # token is re-added afterwards, under GH_TOKEN, and only when it really
    # is the auditor's own.
    for name in AUDITOR_CREDENTIAL_ENV_NAMES:
        env.pop(name, None)

    if credential.present:
        env["GH_TOKEN"] = credential.token

    return env
