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


def _normalize_ruleset_for_compare(payload: dict) -> dict:
    """Project only the fields that matter for the security contract."""
    conditions = payload.get("conditions") or {}
    rules = payload.get("rules") or []
    workflow_rule = next((rule for rule in rules if isinstance(rule, dict) and rule.get("type") == "workflows"), None)
    workflow_params = workflow_rule.get("parameters") if isinstance(workflow_rule, dict) else None
    workflows = workflow_params.get("workflows") if isinstance(workflow_params, dict) else None
    matched_workflow = None
    if isinstance(workflows, list):
        candidates = [w for w in workflows if isinstance(w, dict) and w.get("path") == REQUIRED_WORKFLOW_PATH]
        if candidates:
            matched_workflow = candidates[0]
    return {
        "enforcement": payload.get("enforcement"),
        "conditions": {
            "ref_name": conditions.get("ref_name"),
            "include": (conditions.get("ref_name") or {}).get("include"),
        },
        "workflow": {
            "path": matched_workflow.get("path") if matched_workflow else None,
            "ref": matched_workflow.get("ref") if matched_workflow else None,
            "repository_id": matched_workflow.get("repository_id") if matched_workflow else None,
        },
        "bypass_actors": payload.get("bypass_actors"),
        "has_status_only_rule": any(
            isinstance(rule, dict) and rule.get("type") == "required_status_checks" for rule in rules
        ),
    }


def verify_remote_ruleset(*, owner: str, repo: str, expected_name: str, expected_spec: dict) -> list[str]:
    """List rulesets, find the matching ID, GET the full body, compare field-by-field."""
    errors: list[str] = []
    list_proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/rulesets", "--paginate"],
        capture_output=True,
        text=True,
        check=False,
    )
    if list_proc.returncode != 0:
        return [f"github_api_rulesets_query_failed:{(list_proc.stderr or list_proc.stdout).strip()}"]
    try:
        listed = json.loads(list_proc.stdout or "[]")
    except json.JSONDecodeError:
        return ["github_api_rulesets_invalid_json"]
    if not isinstance(listed, list):
        return ["github_api_rulesets_not_list"]
    matched = [item for item in listed if isinstance(item, dict) and item.get("name") == expected_name]
    if not matched:
        return ["github_remote_ruleset_missing"]
    remote_summary = matched[0]
    ruleset_id = remote_summary.get("id")
    if not isinstance(ruleset_id, int):
        return ["github_remote_ruleset_missing_id"]
    get_proc = subprocess.run(
        ["gh", "api", f"repos/{owner}/{repo}/rulesets/{ruleset_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if get_proc.returncode != 0:
        return [f"github_api_ruleset_get_failed:{(get_proc.stderr or get_proc.stdout).strip()}"]
    try:
        remote_full = json.loads(get_proc.stdout or "{}")
    except json.JSONDecodeError:
        return ["github_api_ruleset_get_invalid_json"]
    if not isinstance(remote_full, dict):
        return ["github_api_ruleset_get_not_object"]

    expected_norm = _normalize_ruleset_for_compare(expected_spec)
    remote_norm = _normalize_ruleset_for_compare(remote_full)

    if remote_norm["enforcement"] != "active":
        errors.append(f"github_remote_enforcement_not_active:{remote_norm['enforcement']}")
    if expected_norm["enforcement"] == "active" and remote_norm["enforcement"] != "active":
        errors.append("github_remote_enforcement_mismatch")

    expected_ref_name = expected_norm["conditions"]["ref_name"]
    remote_ref_name = remote_norm["conditions"]["ref_name"]
    if remote_ref_name != expected_ref_name:
        errors.append(f"github_remote_conditions_ref_name_mismatch:expected={expected_ref_name} got={remote_ref_name}")
    else:
        expected_include = (expected_ref_name or {}).get("include")
        remote_include = (remote_ref_name or {}).get("include")
        if expected_include != remote_include:
            errors.append(f"github_remote_conditions_include_mismatch:expected={expected_include} got={remote_include}")

    expected_wf = expected_norm["workflow"]
    remote_wf = remote_norm["workflow"]
    if remote_wf["path"] != expected_wf["path"]:
        errors.append(f"github_remote_workflow_path_mismatch:expected={expected_wf['path']} got={remote_wf['path']}")
    if remote_wf["ref"] != expected_wf["ref"]:
        errors.append(f"github_remote_workflow_ref_mismatch:expected={expected_wf['ref']} got={remote_wf['ref']}")
    if remote_wf["repository_id"] != expected_wf["repository_id"]:
        errors.append(
            f"github_remote_workflow_repository_id_mismatch:expected={expected_wf['repository_id']} got={remote_wf['repository_id']}"
        )
    if remote_norm["has_status_only_rule"] and not expected_norm["has_status_only_rule"]:
        errors.append("github_remote_has_status_only_rule")
    if remote_norm["bypass_actors"] not in (None, []):
        errors.append(f"github_remote_bypass_actors_not_empty:{remote_norm['bypass_actors']}")

    print(
        json.dumps(
            {
                "ruleset_id": ruleset_id,
                "ruleset_name": expected_name,
                "expected_normalized": expected_norm,
                "remote_normalized": remote_norm,
                "errors": errors,
            },
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "ruleset",
        nargs="?",
        type=Path,
        default=Path(".github/rulesets/protected-docker-posix-gate.json"),
    )
    parser.add_argument("--verify-remote", action="store_true")
    parser.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1])
    args = parser.parse_args(argv)
    try:
        payload = load_ruleset(args.ruleset)
        errors = validate_ruleset(payload, strict=True)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"github_ruleset_invalid: {exc}", file=sys.stderr)
        return 1
    if args.verify_remote:
        if not args.owner or not args.repo:
            errors.append("github_remote_verify_missing_repo")
        else:
            errors.extend(
                verify_remote_ruleset(
                    owner=args.owner,
                    repo=args.repo,
                    expected_name=str(payload.get("name")),
                    expected_spec=payload,
                )
            )
    if errors:
        print("github_ruleset_invalid: " + ", ".join(errors), file=sys.stderr)
        return 1
    print("github_ruleset_valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
