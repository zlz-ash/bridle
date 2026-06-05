"""TestCommandPolicy — validate shell commands in plan tests."""
from __future__ import annotations

import shlex

FORBIDDEN_COMMANDS = frozenset({
    "rm", "del", "rmdir", "remove-item",
    "curl", "wget", "invoke-webrequest",
    "start-process", "format", "shutdown", "scp", "ssh",
})

FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"reset", "checkout", "clean"})

SHELL_OPERATORS = (";", "&&", "||", "|", ">", ">>", "<", "$(", "`")


class TestCommandPolicy:
    @staticmethod
    def validate(command: str) -> list[str]:
        if not command or not str(command).strip():
            return ["Empty test command"]
        cmd = str(command).strip()
        errors: list[str] = []

        for op in SHELL_OPERATORS:
            if op in cmd:
                errors.append(f"Shell operator '{op}' is not allowed")
                break

        lowered = cmd.lower()
        if "powershell" in lowered or lowered.startswith("cmd "):
            errors.append("Shell invocation is not allowed")

        errors.extend(TestCommandPolicy._validate_paths(cmd))

        try:
            tokens = shlex.split(cmd, posix=False)
        except ValueError as exc:
            return errors + [f"Cannot parse command: {exc}"]

        if not tokens:
            return errors + ["Empty test command"]

        errors.extend(TestCommandPolicy._validate_executable(tokens))

        if "requires_network" not in lowered and any(
            net in lowered for net in ("curl", "wget", "http://", "https://")
        ):
            errors.append("Network commands require explicit requires_network approval")

        return errors

    @staticmethod
    def validate_all(commands: list[str]) -> list[str]:
        all_errors: list[str] = []
        for cmd in commands:
            all_errors.extend(TestCommandPolicy.validate(cmd))
        return all_errors

    @staticmethod
    def _validate_paths(cmd: str) -> list[str]:
        return []

    @staticmethod
    def _validate_executable(tokens: list[str]) -> list[str]:
        errors: list[str] = []
        executable = tokens[0].lower().replace("\\", "/").split("/")[-1]

        if executable in FORBIDDEN_COMMANDS:
            errors.append(f"Command '{executable}' is not allowed")
        if executable == "git" and len(tokens) > 1 and tokens[1].lower() in FORBIDDEN_GIT_SUBCOMMANDS:
            errors.append(f"Git subcommand '{tokens[1]}' is not allowed")

        if executable == "pytest":
            return errors
        if executable == "echo":
            return errors
        if executable == "exit":
            return errors

        if executable == "npm":
            lower = [t.lower() for t in tokens]
            if lower not in (["npm", "test"], ["npm", "run", "build"]):
                errors.append('npm only allows "npm test" or "npm run build"')
            return errors

        if executable == "python":
            if len(tokens) < 3 or tokens[1] != "-m" or tokens[2].lower() != "pytest":
                errors.append('python only allows "python -m pytest ..."')
            return errors

        if executable.endswith(".exe"):
            executable = executable[:-4]

        errors.append(f"Command '{executable}' is not in allowlist")
        return errors
