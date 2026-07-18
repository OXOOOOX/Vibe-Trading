"""Regression guard for accidental bare ``raise`` statements.

Python only permits ``raise`` without an exception while an exception handler
is active.  A stray one otherwise turns a successful channel action into the
misleading ``RuntimeError: No active exception to reraise``.
"""

from __future__ import annotations

import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_inside_exception_handler(
    node: ast.Raise, parents: dict[ast.AST, ast.AST]
) -> bool:
    """Return whether a bare raise executes in an enclosing except block."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.ExceptHandler):
            return True
        # A nested function/class does not inherit the outer handler's active
        # exception context, even when it is defined lexically inside one.
        if isinstance(current, _SCOPE_NODES):
            return False
        current = parents.get(current)
    return False


def test_source_has_no_bare_raise_outside_active_exception_handler() -> None:
    invalid: list[str] = []
    for source in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is None:
                if not _is_inside_exception_handler(node, parents):
                    invalid.append(f"{source.relative_to(SOURCE_ROOT.parent)}:{node.lineno}")

    assert not invalid, "bare raise outside an active except handler: " + ", ".join(invalid)
