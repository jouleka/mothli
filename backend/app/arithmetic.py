"""A tiny formal arithmetic grammar. Never eval, name lookup, or general language routing."""

import ast
from fractions import Fraction
import re


def arithmetic_prefix(message):
    """Separate only an exact calculation followed by an explicit second request.

    Never classify arbitrary prose as math, discard the remaining request, or attach
    book citations to a value established by calculation.
    """
    parts = re.fullmatch(r"(.{1,140}?),\s*(?:and|also)\s+(.+)", message, re.IGNORECASE)
    if parts:
        reply = arithmetic_reply(parts[1])
        if reply is not None:
            return reply, parts[2]
    return None


def arithmetic_reply(message):
    match = re.fullmatch(
        r"\s*(?:(what is|what's|calculate|compute)\s+)?([\d\s()+*/.-]+)\s*[?=]?\s*",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    expression = match[2].strip()
    # A bare fraction can be a chapter/page reference. A bare year is not a calculation.
    if len(expression) > 100 or (
        not match[1] and ("/" in expression or not any(c in expression for c in "+*"))
    ):
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        if len(list(ast.walk(tree))) > 32 or not any(
            isinstance(n, ast.BinOp) for n in ast.walk(tree)
        ):
            return None

        def solve(node, depth=0):
            if depth > 8:
                raise ValueError()
            if (
                isinstance(node, ast.Constant)
                and type(node.value) is int
                and abs(node.value) <= 10**9
            ):
                value = Fraction(node.value)
            elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                value = solve(node.operand, depth + 1) * (
                    -1 if isinstance(node.op, ast.USub) else 1
                )
            elif isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
            ):
                left, right = solve(node.left, depth + 1), solve(node.right, depth + 1)
                if isinstance(node.op, ast.Add):
                    value = left + right
                elif isinstance(node.op, ast.Sub):
                    value = left - right
                elif isinstance(node.op, ast.Mult):
                    value = left * right
                else:
                    value = left / right
            else:
                raise ValueError()
            if abs(value.numerator) > 10**12 or value.denominator > 10**9:
                raise ValueError()
            return value

        value = solve(tree.body)
    except ZeroDivisionError:
        return "Division by zero is undefined."
    except (SyntaxError, ValueError, RecursionError):
        return None
    return f"{' '.join(expression.split())} = {value}."
