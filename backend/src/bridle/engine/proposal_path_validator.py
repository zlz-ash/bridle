"""ProposalPathValidator — enforce file path boundary in agent proposals.

Every file_patch path must:
- Be a workspace-relative POSIX path.
- NOT be absolute (C: or D: drive, /root/...).
- NOT contain parent traversal (..).
- NOT use Windows backslash as path separator.
- Be present in the node's declared files list.
"""
from __future__ import annotations


class ProposalPathValidator:
    """Validates all file_patch paths against node.files.

    Errors are collected exhaustively, not fail-fast.
    """

    @staticmethod
    def normalize_workspace_relative(path: str) -> str:
        """Normalize workspace-relative path for comparison/persistence.

        Collapses `./`, redundant slashes, and inline `.` segments. Does NOT
        interpret `..` (paths containing `..` must be rejected by the caller).
        """
        if not isinstance(path, str):
            return ""
        p = path.strip()
        while p.startswith("./"):
            p = p[2:]
        parts = [seg for seg in p.split("/") if seg != "" and seg != "."]
        return "/".join(parts)

    @staticmethod
    def first_offending_patch_path(file_patches: list[dict], node_files: list[str]) -> str | None:
        """Return the path field of the first patch that violates boundary rules."""
        for patch in file_patches:
            if ProposalPathValidator.validate([patch], node_files):
                p = patch.get("path")
                if isinstance(p, str) and p:
                    return p
        return None

    @staticmethod
    def validate(file_patches: list[dict], node_files: list[str]) -> list[str]:
        """Validate every patch path against the boundary rules.

        Returns a list of error messages. An empty list means all valid.
        """
        errors: list[str] = []
        norm_node_set: set[str] = set()
        for nf in node_files or []:
            k = ProposalPathValidator.normalize_workspace_relative(nf)
            if k:
                norm_node_set.add(k)

        for i, patch in enumerate(file_patches):
            path = patch.get("path", "")
            if not path or not isinstance(path, str) or not path.strip():
                errors.append(f"Patch [{i}]: empty path")
                continue

            # Absolute POSIX
            if path.startswith("/"):
                errors.append(
                    f"Patch [{i}]: absolute path '{path}' is not allowed "
                    f"(must be workspace-relative POSIX)"
                )
                continue

            # Windows absolute (C:\ or D:\)
            if len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/"):
                errors.append(
                    f"Patch [{i}]: absolute path '{path}' is not allowed "
                    f"(must be workspace-relative POSIX)"
                )
                continue

            # Backslash bypass
            if "\\" in path:
                errors.append(
                    f"Patch [{i}]: path '{path}' contains backslash "
                    f"(must use forward slash POSIX path)"
                )
                continue

            # Parent traversal
            if ".." in path.split("/"):
                errors.append(
                    f"Patch [{i}]: path '{path}' contains parent traversal '..'"
                )
                continue

            norm_path = ProposalPathValidator.normalize_workspace_relative(path)
            if not norm_path:
                errors.append(f"Patch [{i}]: path '{path}' is empty after normalization")
                continue

            # Must be in node.files
            if norm_path not in norm_node_set:
                errors.append(
                    f"Patch [{i}]: path '{path}' is not in node.files"
                )

        return errors
