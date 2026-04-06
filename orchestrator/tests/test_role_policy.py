from __future__ import annotations

from orchestrator.policy.role_policy import build_role_policy


def test_stage_gate_can_only_write_exact_gate_artifact_path():
    policy = build_role_policy(
        role="stage_gate",
        run_id="run-1",
        cycle_no=1,
        stage_no=1,
        attempt_no=1,
    )
    assert policy.can_write(
        "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md"
    )
    assert not policy.can_write(
        "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md.tmp"
    )


def test_release_gate_can_only_write_exact_release_artifact_paths():
    policy = build_role_policy(
        role="release_gate",
        run_id="run-1",
        cycle_no=1,
        release_no=2,
    )
    assert policy.can_write(
        "/workspace/.autogen/runs/run-1/40-release/release-002/decision.md"
    )
    assert policy.can_write(
        "/workspace/.autogen/runs/run-1/50-rework/release-002/rework-summary.md"
    )
    assert not policy.can_write(
        "/workspace/.autogen/runs/run-1/40-release/release-002/decision.md.bak"
    )


def test_e2e_can_write_evidence_dir_and_start_background_tasks_without_code_write():
    policy = build_role_policy(
        role="e2e",
        run_id="run-1",
        cycle_no=1,
        release_no=2,
    )

    assert not policy.allow_code_write
    assert policy.allow_command
    assert policy.allow_background_tasks
    assert policy.can_write(
        "/workspace/.autogen/runs/run-1/30-reviews/release-002/e2e/report.md"
    )
    assert policy.can_write(
        "/workspace/.autogen/runs/run-1/30-reviews/release-002/e2e/evidence/trace.zip"
    )
    assert not policy.can_write(
        "/workspace/.autogen/runs/run-1/30-reviews/release-002/e2e/evidence"
    )
    assert not policy.can_write("/workspace/src/app.tsx")
