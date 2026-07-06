"""TestCommandPolicy — validate shell commands in plan tests.

The policy is the single source of truth for what argv shape is allowed.
Both validation and execution go through :func:`parse_command_argv`, so the
executor never re-interprets a raw string with shell semantics.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass

FORBIDDEN_COMMANDS = frozenset({
    "rm", "del", "rmdir", "remove-item",
    "curl", "wget", "invoke-webrequest",
    "start-process", "format", "shutdown", "scp", "ssh",
})

FORBIDDEN_GIT_SUBCOMMANDS = frozenset({"reset", "checkout", "clean"})

# Substrings that — if present in the raw command string — would let a shell
# run a second command, redirect I/O, or expand a subcommand. Validated
# against the raw string *before* any argv parsing, so we never rely on shlex
# quirks to neutralise them. ``&`` (single) and newlines are included because
# they are real command separators in cmd.exe / POSIX shells.
SHELL_META_SUBSTRINGS: tuple[str, ...] = (
    ";", "&&", "||", "|", ">", ">>", "<", "$(", "`", "&", "\n", "\r",
)


@dataclass(frozen=True)
class ParsedCommand:
    """Structured argv shared by validation and execution."""

    raw: str
    argv: list[str]


def parse_command_argv(command: str) -> ParsedCommand:
    """Parse a command string into structured argv exactly once.

    Raises ``ValueError`` if the command is empty or cannot be tokenised.
    The same parser is used by :class:`TestCommandPolicy` and
    :class:`bridle.agent.tools.executor.Executor`, so executor and validator
    share one argv source of truth.

    ``shlex`` is used with ``posix=False`` so Windows backslash paths are
    preserved verbatim (``posix=True`` would treat ``\\`` as an escape and
    mangle ``C:\\foo``). The trade-off is that ``posix=False`` keeps
    surrounding quotes as literal characters in each token, so we strip one
    layer of matching surrounding quotes here — both the policy and the
    executor see clean argv.
    """
    cmd = str(command).strip()
    if not cmd:
        raise ValueError("Empty test command")
    raw_argv = shlex.split(cmd, posix=False)
    if not raw_argv:
        raise ValueError("Empty test command")
    argv = [_strip_surrounding_quotes(tok) for tok in raw_argv]
    return ParsedCommand(raw=cmd, argv=argv)


def _strip_surrounding_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
        return token[1:-1]
    return token


class TestCommandPolicy:
    @staticmethod
    def validate(command: str) -> list[str]:
        if not command or not str(command).strip():
            return ["Empty test command"]
        cmd = str(command).strip()
        errors: list[str] = []

        for op in SHELL_META_SUBSTRINGS:
            if op in cmd:
                errors.append(f"Shell operator '{op}' is not allowed")
                break

        lowered = cmd.lower()
        if "powershell" in lowered or lowered.startswith("cmd "):
            errors.append("Shell invocation is not allowed")

        errors.extend(TestCommandPolicy._validate_paths(cmd))

        try:
            parsed = parse_command_argv(cmd)
        except ValueError as exc:
            return errors + [f"Cannot parse command: {exc}"]

        errors.extend(TestCommandPolicy._validate_executable(parsed.argv))

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
