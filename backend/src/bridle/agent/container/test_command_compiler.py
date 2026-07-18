"""Compile approved test commands for container entrypoint manifests."""
from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bridle.agent.container.candidate_contract import (
    FrozenTestContract,
    TestCaseSnapshot,
    TestCommandSnapshot,
    TestFileSnapshot,
    file_sha256,
)
from bridle.agent.tools.proposal_path_validator import ProposalPathValidator
from bridle.agent.tools.test_command_policy import TestCommandPolicy


@dataclass(frozen=True)
class ApprovedTestCommand:
    command_id: str
    argv: tuple[str, ...]
    raw_command: str
    test_entity_id: str
    map_seq: int


class TestCommandCompiler:
    """Turn map test entities into stable command IDs and argv lists."""

    @staticmethod
    def compile_commands(
        *,
        test_commands: list[str],
        test_entity_id: str,
        map_seq: int,
    ) -> list[ApprovedTestCommand]:
        approved: list[ApprovedTestCommand] = []
        for raw in test_commands:
            text = str(raw).strip()
            if not text:
                continue
            errors = TestCommandPolicy.validate(text)
            if errors:
                raise ValueError(f"test_target_not_allowed: {errors[0]}")
            argv = tuple(shlex.split(text, posix=False))
            command_id = hashlib.sha256(f"{test_entity_id}:{map_seq}:{text}".encode()).hexdigest()[:16]
            approved.append(
                ApprovedTestCommand(
                    command_id=command_id,
                    argv=argv,
                    raw_command=text,
                    test_entity_id=test_entity_id,
                    map_seq=map_seq,
                )
            )
        return approved

    @staticmethod
    def manifest_commands(commands: list[ApprovedTestCommand]) -> list[dict[str, Any]]:
        return [
            {
                "command_id": cmd.command_id,
                "argv": list(cmd.argv),
                "raw_command": cmd.raw_command,
                "test_entity_id": cmd.test_entity_id,
                "map_seq": cmd.map_seq,
            }
            for cmd in commands
        ]

    @staticmethod
    def freeze_contract(
        *,
        project_root: str | Path,
        test_files: list[str],
        test_cases: list[str],
        test_commands: list[str],
        expected_failure_cases: list[str],
        test_entity_id: str,
        baseline_hash: str,
        map_seq: int,
        boundary_fingerprint: str,
        image_version: str,
    ) -> FrozenTestContract:
        root = Path(project_root).resolve()
        file_snapshots: list[TestFileSnapshot] = []
        for raw_path in test_files:
            normalized = ProposalPathValidator.normalize_workspace_relative(str(raw_path))
            target = (root / Path(*normalized.split("/"))).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError("test_contract_file_outside_project") from exc
            if not normalized or not target.is_file():
                raise ValueError(f"test_contract_file_missing:{normalized or raw_path}")
            file_snapshots.append(TestFileSnapshot(path=normalized, sha256=file_sha256(target)))

        cases: list[TestCaseSnapshot] = []
        case_ids_by_node: dict[str, str] = {}
        for raw_case in test_cases:
            node_id = str(raw_case).strip().replace("\\", "/")
            if not node_id:
                continue
            case_id = hashlib.sha256(f"{test_entity_id}:{node_id}".encode()).hexdigest()[:16]
            previous = case_ids_by_node.setdefault(node_id, case_id)
            if previous != case_id:
                raise ValueError("test_contract_case_id_conflict")
            cases.append(TestCaseSnapshot(case_id=case_id, node_id=node_id))

        expected_ids: list[str] = []
        for raw_case in expected_failure_cases:
            node_id = str(raw_case).strip().replace("\\", "/")
            case_id = case_ids_by_node.get(node_id)
            if case_id is None:
                raise ValueError(f"test_contract_expected_failure_case_unknown:{node_id}")
            expected_ids.append(case_id)

        approved = TestCommandCompiler.compile_commands(
            test_commands=test_commands,
            test_entity_id=test_entity_id,
            map_seq=map_seq,
        )
        commands = tuple(
            TestCommandSnapshot(
                command_id=item.command_id,
                argv=item.argv,
                raw_command=item.raw_command,
                test_entity_id=item.test_entity_id,
                map_seq=item.map_seq,
            )
            for item in approved
        )
        return FrozenTestContract.freeze(
            test_files=tuple(file_snapshots),
            cases=tuple(cases),
            commands=commands,
            expected_failure_case_ids=tuple(expected_ids),
            baseline_hash=baseline_hash,
            map_seq=map_seq,
            boundary_fingerprint=boundary_fingerprint,
            image_version=image_version,
        )
