Derived from the installed MIT-licensed skill `currents-dev/playwright-best-practices-skill@playwright-best-practices`.

- Prefer repo-owned Playwright coverage first. If the repo already exposes `test:e2e`, `playwright test`, or equivalent scripts, use those before inventing helper code.
- Keep the validation loop tight: install dependencies once, build once if needed, start one app server with `task_create`, wait for readiness with `task_output`, run the focused acceptance path, then stop the task.
- Prefer observable user journeys over tool archaeology. A narrow failing scenario with concrete evidence is better than many turns of environment probing.
- Limit environment diagnosis to a few targeted checks such as `bun --version`, Playwright version, config/test file presence, and server readiness logs.
- If bootstrapping still fails after targeted checks, capture the exact blocker and return a failing verdict instead of continuing open-ended debugging.
- When repo-owned tests fail, preserve the first useful failure artifact: trace, screenshot, HTML output, or server logs.
- If helper scripts are unavoidable, write them only under `evidence_dir` and keep them small, scenario-focused, and disposable.
- Avoid brittle sleeps and broad retries. Prefer Playwright's built-in waiting, resilient assertions, and stable user-facing flows.
- Reports should name the scenario exercised, the exact command or script used, the observed behavior, and where the evidence was written.
