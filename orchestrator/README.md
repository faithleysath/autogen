## Orchestrator

This package implements the `v1` orchestration baseline described in:

- `/Users/laysath/proj/autogen/docs/v1-implementation-design.md`
- `/Users/laysath/proj/autogen/docs/v1-runtime-architecture.md`

### What is implemented

- LangGraph top-level workflow with:
  - run initialization
  - planning cycle
  - stage loop with developer + stage gate semantics
  - release candidate freeze
  - three review branches
  - publisher-style serial report publication
  - release gate
  - replan loop
- Shared workspace model with `/workspace` visibility and backing-path isolation
- SQLite-backed push locks and LangGraph checkpoint persistence
- Docker-backed execution containers for dev and e2e roles
- Git service that performs clone / checkout / commit / push inside execution containers
- OpenAI Responses API role runner with restricted file, artifact, git-read, and command tools

### CLI

```bash
cd /Users/laysath/proj/autogen/orchestrator
uv sync --all-extras
uv run orchestrator --help
uv run orchestrator graph --thread-id graph-preview
uv run orchestrator run \
  --repo-url git@github.com:your-org/your-repo.git \
  --prd-file /absolute/path/to/prd.md \
  --thread-id demo-run-001
```

You can run the orchestrator directly on your local machine. Docker Compose is not required for the orchestrator process itself, but Docker is still required because role workspaces run inside the `autogen-agent-dev` and `autogen-agent-e2e` containers.

### Required runtime inputs

- `AUTOGEN_WORKSPACE_ROOT`
- `OPENAI_API_KEY` for live role execution
- Docker daemon access from the orchestrator container or local process
- Built `autogen-agent-dev` and `autogen-agent-e2e` images

### OpenAI-compatible providers

The role runner accepts OpenAI-compatible endpoints through `OPENAI_BASE_URL`.

- Use `AUTOGEN_OPENAI_API_MODE=responses` for providers that support the OpenAI Responses API.
- Use `AUTOGEN_OPENAI_API_MODE=chat_completions` for providers that expose only `/v1/chat/completions`.
- Some Responses-compatible providers do not support `previous_response_id`; for those, enable stateless response replay with `AUTOGEN_OPENAI_STATELESS_RESPONSES=true`.

Example local run against a generic Responses-compatible provider:

```bash
cd /Users/laysath/proj/autogen/orchestrator

export AUTOGEN_WORKSPACE_ROOT=/absolute/path/to/workspaces
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://provider.example.com/v1
export AUTOGEN_OPENAI_API_MODE=responses
export AUTOGEN_OPENAI_STATELESS_RESPONSES=true
export AUTOGEN_OPENAI_RESPONSE_TIMEOUT_SECONDS=90
export AUTOGEN_MODEL_DEFAULT=gpt-5.4-xhigh

uv run orchestrator run \
  --repo-url git@github.com:your-org/your-repo.git \
  --prd-file /absolute/path/to/prd.md \
  --thread-id demo-run-compatible-provider
```

Example local run against Right Codes `chat/completions`:

```bash
cd /Users/laysath/proj/autogen/orchestrator

export AUTOGEN_WORKSPACE_ROOT=/absolute/path/to/workspaces
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://www.right.codes/codex/v1
export AUTOGEN_OPENAI_API_MODE=chat_completions
export AUTOGEN_OPENAI_RESPONSE_TIMEOUT_SECONDS=180
export AUTOGEN_MODEL_DEFAULT=gpt-5.4-xhigh

uv run orchestrator run \
  --repo-url git@github.com:your-org/your-repo.git \
  --prd-file /absolute/path/to/prd.md \
  --thread-id demo-run-rightcodes
```

For `gpt-5.4-xhigh`, a 45 second timeout is often too small once the developer role starts planning or scaffolding. Prefer `AUTOGEN_OPENAI_RESPONSE_TIMEOUT_SECONDS=180` or higher for long tool loops.

### Current verification

- `python -m compileall src`
- `uv run pytest`
- `uv run orchestrator --help`
- `uv run orchestrator graph --thread-id graph-preview`

### Observability

- Runtime logs are emitted as JSON lines to stderr.
- The same logs are persisted under `${AUTOGEN_STATE_DIR:-$AUTOGEN_WORKSPACE_ROOT/_state}/logs/orchestrator/<thread_id>.log`.
- LangSmith is now explicitly integrated rather than relying only on environment passthrough.
- When `LANGSMITH_TRACING=true` and valid LangSmith credentials are set, the orchestrator creates traces for:
  - the root orchestrator run
  - graph nodes
  - role executions
  - tool calls
  - wrapped OpenAI Responses API calls
- Current log coverage includes:
  - graph node start / success / failure
  - workspace creation and cleanup
  - artifact reads / writes / copies
  - push lock acquire / release
  - Docker container create / exec / remove
  - git clone / checkout / pull / commit / push
  - role runner start / tool call / completion
- JSON logs automatically include `langsmith_run_id` and `langsmith_trace_id` whenever a log line is emitted inside an active LangSmith trace context.
