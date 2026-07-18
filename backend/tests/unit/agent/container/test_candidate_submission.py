from __future__ import annotations

from pathlib import Path

import pytest

from bridle.agent.container.candidate_contract import compute_patches
from bridle.agent.container.candidate_service import CandidateExecutionService
from bridle.agent.runtime.change_outbox import (
    AtomicPatchCommitter,
    ChangeCorrelation,
    ChangeOutbox,
)


def _prepare_candidate(root: Path):
    files = {
        "src/a.py": "A = 1\n",
        "src/old_name.py": "RENAMED = True\n",
        "src/remove_me.py": "REMOVE = True\n",
    }
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    service = CandidateExecutionService(root)
    setup = service.prepare_from_snapshot(
        {
            "module_id": "module-submission",
            "node_id": "node-submission",
            "implementation_entities": [
                {"entity_id": f"entity-{index}", "path": relative}
                for index, relative in enumerate(files, start=1)
            ],
            "test_entities": [],
            "test_commands": ["python -m pytest -q"],
            "interfaces": [],
        },
        run_id="run-submission",
        candidate_id="candidate-submission",
        base_map_seq=17,
    )
    return service, setup


def test_submission_revision_and_atomic_publish_lifecycle(test_workspace: Path) -> None:
    service, setup = _prepare_candidate(test_workspace)
    project = setup.workspace.project_dir
    (project / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")

    first = service.freeze_submission(setup)

    assert first.candidate_id == setup.candidate_id
    assert first.revision == 1
    assert first.submission_id
    assert first.candidate_tree_hash != first.base_tree_hash
    assert first.changed_paths == ("src/a.py",)
    assert service.validate_submission(setup, first).status == "valid"

    (project / "src" / "a.py").write_text("A = 3\n", encoding="utf-8")
    drifted = service.validate_submission(setup, first)
    assert drifted.status == "invalid"
    assert drifted.error_code == "candidate_submission_changed"

    second = service.freeze_submission(setup)
    assert second.revision == 2
    assert second.submission_id != first.submission_id
    assert second.candidate_tree_hash != first.candidate_tree_hash

    restarted = CandidateExecutionService(test_workspace)
    third = restarted.freeze_submission(setup)
    assert third.revision == 3
    assert restarted.load_submission(setup, third.submission_id) == third

    _assert_rename_patch_contract()
    _assert_batch_publish_contract(test_workspace)


def _assert_rename_patch_contract() -> None:
    changed, patches = compute_patches(
        base_hashes={"src/old.py": "same", "src/keep.py": "before"},
        candidate_hashes={"src/new.py": "same", "src/keep.py": "after"},
        write_set=["src/old.py", "src/new.py", "src/keep.py"],
    )

    assert changed == ["src/keep.py", "src/new.py", "src/old.py"]
    assert patches == [
        {
            "path": "src/keep.py",
            "change_type": "modify",
            "base_hash": "before",
            "candidate_hash": "after",
        },
        {
            "old_path": "src/old.py",
            "path": "src/new.py",
            "change_type": "rename",
            "base_hash": "same",
            "candidate_hash": "same",
        },
    ]


def _assert_batch_publish_contract(
    test_workspace: Path,
) -> None:
    originals = {
        "src/a.py": "A = 1\n",
        "src/old.py": "RENAMED = True\n",
        "src/remove.py": "REMOVE = True\n",
    }
    for relative, text in originals.items():
        target = test_workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def fail_second_replace(stage, intent) -> None:
        if stage == "after_replace" and intent.relative_path == "src/new.py":
            raise OSError("injected batch publish failure")

    correlation = ChangeCorrelation(
        trace_id="trace-publish",
        project_id="project-publish",
        agent_id="agent-publish",
        generation=1,
    )
    changes = [
        {"path": "src/a.py", "change_type": "modify", "new_text": "A = 2\n"},
        {
            "old_path": "src/old.py",
            "path": "src/new.py",
            "change_type": "rename",
            "new_text": "RENAMED = True\n",
        },
        {"path": "src/remove.py", "change_type": "remove", "new_text": None},
        {"path": "src/added.py", "change_type": "add", "new_text": "ADDED = True\n"},
    ]
    failing = AtomicPatchCommitter(
        ChangeOutbox(
            test_workspace,
            project_id="project-publish",
            failure_hook=fail_second_replace,
        )
    )

    failed = failing.commit_many(changes, correlation=correlation)

    assert failed.status == "failed"
    assert failed.error_code == "candidate_publish_failed"
    for relative, text in originals.items():
        assert (test_workspace / relative).read_text(encoding="utf-8") == text
    assert not (test_workspace / "src" / "new.py").exists()
    assert not (test_workspace / "src" / "added.py").exists()

    successful = AtomicPatchCommitter(
        ChangeOutbox(test_workspace, project_id="project-publish")
    ).commit_many(changes, correlation=correlation)

    assert successful.status == "ready"
    assert [intent.change_type for intent in successful.intents] == [
        "modify",
        "rename",
        "remove",
        "add",
    ]
    assert (test_workspace / "src" / "a.py").read_text(encoding="utf-8") == "A = 2\n"
    assert not (test_workspace / "src" / "old.py").exists()
    assert (test_workspace / "src" / "new.py").read_text(encoding="utf-8") == "RENAMED = True\n"
    assert not (test_workspace / "src" / "remove.py").exists()
    assert (test_workspace / "src" / "added.py").read_text(encoding="utf-8") == "ADDED = True\n"
