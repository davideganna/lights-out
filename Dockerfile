# The full image already has git, make, curl, procps, tzdata and a C toolchain,
# so no apt step is needed. Toolchains available to the coding agent: add more
# here if the target repository needs them (and mirror them in its CI workflow).
FROM python:3.12

# Node.js from the official image (Debian's npm package drags in hundreds of deps).
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN useradd --create-home --shell /bin/bash agent \
 && git config --system safe.directory '*'

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Expose the Claude Code CLI bundled with the Agent SDK, and the wrapper that
# drops privileges before starting it.
RUN ln -s "$(python -c 'import claude_agent_sdk, os; print(os.path.dirname(claude_agent_sdk.__file__))')/_bundled/claude" /usr/local/bin/claude
COPY docker/claude-as-agent /usr/local/bin/claude-as-agent
RUN chmod 755 /usr/local/bin/claude-as-agent

ENV DATA_DIR=/data \
    AGENT_USER=agent \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --retries=3 CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('REPORT_PORT', '8080'), timeout=5)"]
ENTRYPOINT ["lights-out"]
CMD ["run"]
