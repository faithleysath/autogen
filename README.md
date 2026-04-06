## Local Stack

This repository currently includes:

- `orchestrator`: the controller container that will manage agent runs
- `agent-dev`: a Debian-based Bun frontend development image template for coding and git operations
- `agent-e2e`: a Debian-based Bun + Playwright frontend validation image template for browser validation

Observability is configured to use the hosted LangSmith service instead of a self-hosted stack.

## Minimal Env

The smallest useful local setup is documented in [.env.example](/Users/laysath/proj/autogen/.env.example).

For the current `docker-compose.yml`, the important variables are:

- `LANGSMITH_TRACING=true`: turns on tracing
- `LANGSMITH_API_KEY`: required to send traces to your hosted LangSmith account
- `LANGSMITH_PROJECT`: optional project name for traces
- `LANGSMITH_WORKSPACE_ID`: only needed if your API key belongs to multiple workspaces
- `OPENAI_API_KEY`: likely needed once the orchestrator starts making model calls
- `AGENT_SSH_DIR`: optional local directory with SSH material for agent containers

Notes:

- If you want a custom tracing project, set `LANGSMITH_PROJECT`; otherwise LangSmith will use its default tracing project behavior.

## Local SSH For GitHub

This repo now reserves `.secrets/ssh/` for local-only SSH material used by the experimental agent containers.

- The path is ignored by git.
- The images already include GitHub's published host keys in `/etc/ssh/ssh_known_hosts`.
- A minimal setup is now just to place `id_ed25519` in `.secrets/ssh/`.
- `known_hosts` in `.secrets/ssh/` is only needed if you want to override or extend the default trust set.
- If you want to force a specific identity, add a `.secrets/ssh/config` with a `github.com` host entry.
- If neither the mounted SSH directory nor the image-level global `known_hosts` contains `github.com`, the container will still try to populate `known_hosts` with `ssh-keyscan github.com` at startup.

Example:

```sshconfig
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
```

## Compose

The main entrypoint is [docker-compose.yml](/Users/laysath/proj/autogen/docker-compose.yml).

`agent-dev` and `agent-e2e` are intentionally behind the `manual-agents` profile. Plain `docker compose up` should focus on `orchestrator`; the agent containers are meant to be created dynamically by the orchestrator at runtime. The profile exists only so we can build and manually inspect the base images during development.

These two agent images are now intentionally scoped to Bun-based frontend work. They do not preinstall Python or `uv`; the Python runtime remains isolated to the dedicated [orchestrator](/Users/laysath/proj/autogen/orchestrator) container.

Only the `orchestrator` container gets access to the host Docker socket. `agent-dev` and `agent-e2e` no longer include Docker CLI tooling and cannot control sibling containers directly.

The `manual-agents` profile also no longer bind-mounts the host repository into `/workspace`; these containers are now for image inspection and SSH validation rather than in-place repo editing.

The image definitions live in:

- [docker/agents/dev.Dockerfile](/Users/laysath/proj/autogen/docker/agents/dev.Dockerfile)
- [docker/agents/e2e.Dockerfile](/Users/laysath/proj/autogen/docker/agents/e2e.Dockerfile)

Useful commands:

```bash
docker compose config
docker compose build agent-dev agent-e2e orchestrator
docker compose up -d
docker compose --profile manual-agents up -d agent-dev agent-e2e
docker compose --profile manual-agents exec agent-dev sh
docker compose --profile manual-agents exec agent-dev ssh -T git@github.com
docker compose logs -f orchestrator
docker compose down
```
