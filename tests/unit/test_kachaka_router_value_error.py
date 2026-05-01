"""Pin the global RobotNotRegistered -> 404 handler contract.

main.py installs a single exception_handler so kachaka.py endpoints can call
FleetAPI directly without each wrapping a try/except. This guards both halves
of the contract: the handler stays registered, and kachaka.py stays free of
ad-hoc ValueError catches.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.fleet_api import RobotNotRegistered


REPO_ROOT = Path(__file__).resolve().parents[2]
KACHAKA_ROUTER = REPO_ROOT / "src" / "backend" / "routers" / "kachaka.py"
MAIN_PY = REPO_ROOT / "src" / "backend" / "main.py"


def test_robot_not_registered_subclasses_value_error():
    """Existing 'except ValueError' callers (e.g. task_runtime._execute_step)
    still match — subclassing keeps back-compat."""
    assert issubclass(RobotNotRegistered, ValueError)


def _names_caught(handler: ast.ExceptHandler) -> list[str]:
    """Return the simple names caught by an except clause (handles tuples)."""
    type_node = handler.type
    if type_node is None:
        return ["BaseException"]
    if isinstance(type_node, ast.Name):
        return [type_node.id]
    if isinstance(type_node, ast.Tuple):
        return [n.id for n in type_node.elts if isinstance(n, ast.Name)]
    return []


def test_kachaka_router_does_not_catch_value_error():
    """AST-based: kachaka.py must not contain ``except ValueError`` blocks
    in any function — those are now handled by main.py's global handler."""
    tree = ast.parse(KACHAKA_ROUTER.read_text(encoding="utf-8"))
    offenders = [
        f"line {handler.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        for handler in [node]
        if "ValueError" in _names_caught(node)
    ]
    assert not offenders, f"kachaka.py still catches ValueError at: {offenders}"


def test_main_registers_robot_not_registered_handler():
    """AST-based: main.py must decorate a function with
    ``@app.exception_handler(RobotNotRegistered)`` (not bare ValueError)."""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            # @app.exception_handler(X)
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "exception_handler"
                and len(deco.args) == 1
                and isinstance(deco.args[0], ast.Name)
                and deco.args[0].id == "RobotNotRegistered"
            ):
                found = True
                break
    assert found, "main.py must register @app.exception_handler(RobotNotRegistered)"
