# The engine and every program its scripts call, in one image.
# Nothing of yours is inside: settings, plugins and data are attached when it runs (see compose.yaml).
FROM python:3.12-slim

# Programs that stage scripts, tools and MCP servers call. Add yours to this line.
# nodejs and npm are here for the coding CLIs below, and for MCP servers started with npx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# The coding CLIs a model entry in agentflow.yaml can name with `harness:`, each at a fixed version.
# Take out the one you do not use; `agentflow check` says which are installed.
RUN npm install -g @openai/codex@0.166.0 @anthropic-ai/claude-code@2.1.277 \
    && npm cache clean --force

WORKDIR /opt/agentflow
COPY pyproject.toml ./
COPY agentflow ./agentflow
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 agentflow && mkdir -p /work/data && chown -R agentflow /work
USER agentflow
ENV AGENTFLOW_HOME=/work
WORKDIR /work
EXPOSE 8321
ENTRYPOINT ["agentflow"]
CMD ["serve", "--host", "0.0.0.0"]
