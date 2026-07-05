"""tree-sitter structural indexing for the Bridle code map."""
from __future__ import annotations

from bridle.features.project_map.indexer.scip_indexer import ScipIndexer, ScipIndexResult
from bridle.features.project_map.indexer.treesitter_indexer import (
    IndexResult,
    TreeSitterIndexer,
    classify_is_test,
)

__all__ = [
    "IndexResult",
    "ScipIndexResult",
    "ScipIndexer",
    "TreeSitterIndexer",
    "classify_is_test",
]
