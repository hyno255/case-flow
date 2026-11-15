# The engine and every program its scripts call, in one image.
# Nothing of yours is inside: settings, plugins and data are attached when it runs (see compose.yaml).
FROM python:3.12-slim

# Programs that stage scripts, tools and MCP servers call. Add yours to this line,
# for example `nodejs npm` for MCP servers started with npx.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
