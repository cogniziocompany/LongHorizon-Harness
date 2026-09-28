# lh-harness-node — LongHorizon-Harness as a Hydra-managed fleet node.
#
# Linux-only by design: LocalEnvironment.exec uses os.killpg/SIGHUP and is
# unrunnable on native Windows (cognizioware-how-to.md §2). On Windows devices
# this container (Docker Desktop / WSL2 backend) IS the supported path.
#
# Build from the repo root:
#   docker build -f docker/Dockerfile -t lh-harness-node:latest .

FROM python:3.12-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# git + ssh for repo checkouts, curl for healthchecks, node 22 for the
# claude_code adapter (the harness spawns the `claude` CLI per role turn).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git openssh-client curl ca-certificates gnupg procps \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Non-root, mirrors the CT110 deployment user.
RUN useradd -m -u 1001 -s /bin/bash harness

# Install the harness from this repo checkout (source of truth, like the
# WSL2 -e install — rebuild the image to pick up repo edits).
COPY --chown=harness:harness . /opt/lh-harness-src
RUN pip install /opt/lh-harness-src

COPY --chown=harness:harness docker/entrypoint.sh /usr/local/bin/lh-node-entrypoint
RUN chmod +x /usr/local/bin/lh-node-entrypoint
COPY --chown=harness:harness docker/materialize_node_config.py /usr/local/bin/lh-node-materialize-config
RUN chmod +x /usr/local/bin/lh-node-materialize-config

USER harness
WORKDIR /home/harness

# Fleet-node conventions (overridable at run time).
ENV LH_NODE_WORKSPACE_ROOT=/home/harness/work \
    LH_NODE_RUNS_ROOT=/home/harness/runs \
    LH_NODE_PORT=8799

EXPOSE 8799

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS -H "Authorization: Bearer ${LH_HARNESS_WEB_TOKEN}" \
        "http://127.0.0.1:${LH_NODE_PORT}/api/meta" > /dev/null || exit 1

ENTRYPOINT ["lh-node-entrypoint"]
