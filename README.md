## Local Stack

This repository currently includes:

- `orchestrator`: the controller container that will manage agent runs

Observability is configured to use the hosted LangSmith service instead of a self-hosted stack.

## Minimal Env

The smallest useful local setup is documented in [.env.example](/Users/laysath/proj/autogen/.env.example).

For the current `docker-compose.yml`, the important variables are:

- `LANGSMITH_TRACING=true`: turns on tracing
- `LANGSMITH_API_KEY`: required to send traces to your hosted LangSmith account
- `LANGSMITH_PROJECT`: optional project name for traces
- `LANGSMITH_WORKSPACE_ID`: only needed if your API key belongs to multiple workspaces
- `OPENAI_API_KEY`: likely needed once the orchestrator starts making model calls

Notes:

- For hosted LangSmith, the default service endpoint is usually enough, so `LANGSMITH_ENDPOINT` can stay empty.
- If you want a custom tracing project, set `LANGSMITH_PROJECT`; otherwise LangSmith will use its default tracing project behavior.

## Compose

The main entrypoint is [docker-compose.yml](/Users/laysath/proj/autogen/docker-compose.yml).

Useful commands:

```bash
docker compose config
docker compose up -d
docker compose logs -f orchestrator
docker compose down
```
