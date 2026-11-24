"""Static log-policy and content-free audit vocabulary gates for LIT-49."""

from __future__ import annotations

import ast
from pathlib import Path

from app.hosted.audit import APPROVED_SECURITY_LOG_MESSAGES

APP = Path(__file__).parents[2] / "app"


def _message_tables(tree: ast.Module) -> dict[str, list[object]]:
    """Module-level dicts of messages (``NAME = {key: "message", ...}``): a log call may pick its
    message from one by key, and then every value in it must be a reviewed static message."""
    tables = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Dict)
        ):
            tables[node.targets[0].id] = [
                value.value if isinstance(value, ast.Constant) else None
                for value in node.value.values
            ]
    return tables


def test_hosted_application_logs_use_only_reviewed_static_messages() -> None:
    files = [APP / "main.py", *(APP / "hosted").rglob("*.py")]
    messages = set()
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tables = _message_tables(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOG"
                and node.func.attr in {"debug", "info", "warning", "error", "critical", "exception"}
            ):
                continue
            assert node.args
            first = node.args[0]
            if isinstance(first, ast.Subscript):
                # A message chosen from a fixed table, with nothing else passed.
                assert isinstance(first.value, ast.Name) and first.value.id in tables
                assert len(node.args) == 1
                found = tables[first.value.id]
            else:
                assert isinstance(first, ast.Constant)
                found = [first.value]
            for message in found:
                assert isinstance(message, str)
                assert message in APPROVED_SECURITY_LOG_MESSAGES
                messages.add(message)
            assert node.func.attr != "exception"
            assert not {keyword.arg for keyword in node.keywords} & {
                "exc_info",
                "stack_info",
                "extra",
            }
            for argument in node.args[1:]:
                assert (
                    isinstance(argument, ast.Subscript)
                    and isinstance(argument.value, ast.Name)
                    and argument.value.id == "stats"
                    and isinstance(argument.slice, ast.Constant)
                    and argument.slice.value == "active_lock_leases"
                )
    assert messages == set(APPROVED_SECURITY_LOG_MESSAGES)
