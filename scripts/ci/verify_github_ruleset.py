#!/usr/bin/env python3
"""Verify local ruleset spec and optionally compare with GitHub API."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_WORKFLOW_PATH = ".github/workflows/container-docker-linux.yml"
DEFAULT_WORKFLOW_REF = "refs/heads/master"
MALICIOUS_PR_SCENARIOS = (
    "validator_always_passes",
    "critical_tests_assert_true",
    "skip_docker_write_json",
    "forge_digest_outputs",
)


def load_ruleset(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ruleset_must_be_object")
    return payload


def validate_ruleset(payload: dict, *, strict: bool = True) -> list[str]:
    errors: list[str] = []
    conditions = payload.get("conditions")
    if not isinstance(conditions, dict):
        return ["conditions_missing"]
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict) or ref_name.get("include") != ["~DEFAULT_BRANCH"]:
        errors.append("ref_name_must_target_default_branch")
    rules = payload.get("rules")
    if not isinstance(rules, list):
        return errors + ["rules_missing"]

    workflow_rule = next((rule for rule in rules if rule.get("type") == "workflows"), None)
    if not isinstance(workflow_rule, dict):
        errors.append("required_workflows_missing")
    else:
        params = workflow_rule.get("parameters")
        workflows = params.get("workflows") if isinstance(params, dict) else None
        if not isinstance(workflows, list) or not workflows:
            errors.append("required_workflows_list_missing")
        else:
            matched = [
                item
                for item in workflows
                if isinstance(item, dict) and item.get("path") == REQUIRED_WORKFLOW_PATH
            ]
            if not matched:
                errors.append("required_workflow_path_mismatch")
            else:
                binding = matched[0]
                ref = binding.get("ref")
                if not isinstance(ref, str) or not ref.startswith("refs/"):
                    errors.append("required_workflow_ref_missing")
                elif strict and ref != DEFAULT_WORKFLOW_REF:
                    errors.append("required_workflow_ref_mismatch")
                repository_id = binding.get("repository_id")
                if strict:
                    if repository_id is None:
                        errors.append("required_workflow_repository_id_missing")
                    elif not isinstance(repository_id, int) or repository_id <= 0:
                        errors.append("required_workflow_repository_id_invalid")
                elif repository_id is not None and (
                    not isinstance(repository_id, int) or repository_id <= 0
                ):
                    errors.append("required_workflow_repository_id_invalid")

    status_only = next((rule for rule in rules if rule.get("type") == "required_status_checks"), None)
    if isinstance(status_only, dict):
        errors.append("status_only_checks_not_sufficient")

    metadata = payload.get("metadata") or {}
    if metadata.get("required_workflow_file") and not isinstance(workflow_rule, dict):
        errors.append("metadata_workflow_hint_without_workflows_rule")
    if metadata.get("required_workflow_path") != REQUIRED_WORKFLOW_PATH:
        errors.append("metadata_required_workflow_path_mismatch")
    if metadata.get("required_workflow_ref") not in (None, DEFAULT_WORKFLOW_REF):
        errors.append("metadata_required_workflow_ref_mismatch")
    scenarios = metadata.get("malicious_pr_scenarios")
    if scenarios != list(MALICIOUS_PR_SCENARIOS):
        errors.append("malicious_pr_scenarios_mismatch")
    if payload.get("enforcement") != "active":
        errors.append("enforcement_must_be_active")
    if payload.get("bypass_actors") not in (None, []):
        errors.append("bypass_actors_must_be_empty")
    return errors


def verify_remote_ruleset(*, owner: str, repo: str, expected_name: str) -> list[str]:
    errors: list[str] = []
    proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/rulesets", "--paginate"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return [f"github_api_rulesets_query_failed:{proc.stderr.strip() or proc.stdout.strip()}"]
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return ["github_api_rulesets_invalid_json"]
    if not isinstance(payload, list):
        return ["github_api_rulesets_not_list"]
    matched = [item for item in payload if isinstance(item, dict) and item.get("name") == expected_name]
    if not matched:
        errors.append("github_remote_ruleset_missing")
        return errors
    remote = matched[0]
    if remote.get("enforcement") != "active":
        errors.append("github_remote_ruleset_not_active")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ruleset",
        nargs="?",
        type=Path,
        default=Path(".github/rulesets/protected-docker-posix-gate.json"),
    )
    parser.add_argument(
        "--relaxed",
        action="store_true",
        help="Allow placeholder repository_id in local template checks.",
    )
    parser.add_argument("--verify-remote", action="store_true")
    parser.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1])
    args = parser.parse_args(argv)
    strict = not args.relaxed and os.environ.get("BRIDLE_RULESET_RELAXED") != "1"
    try:
        payload = load_ruleset(args.ruleset)
        errors = validate_ruleset(payload, strict=strict)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"github_ruleset_invalid: {exc}", file=sys.stderr)
        return 1
    if args.verify_remote:
        if not args.owner or not args.repo:
            errors.append("github_remote_verify_missing_repo")
        else:
            errors.extend(verify_remote_ruleset(owner=args.owner, repo=args.repo, expected_name=str(payload.get("name"))))
    if errors:
        print("github_ruleset_invalid: " + ", ".join(errors), file=sys.stderr)
        return 1
    print("github_ruleset_valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
