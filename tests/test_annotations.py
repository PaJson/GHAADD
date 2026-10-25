"""Every function has typed parameters and a return type (the "Typing annotations required" rule in CLAUDE.md).

Pyright's reportMissingParameterType (pyrightconfig.json) covers the parameters; Pyright has no rule for a missing
return type, so this test reads the source files itself. Run from the project root: python -m unittest discover -s tests -t .
"""
import ast
import os
import unittest
from typing import Iterator, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE_FOLDERS = ("modules", "viewer", "tests")
SOURCE_FILES = ("main.py", "main_gui.py")


def python_files() -> Iterator[str]:
    """Yield the path of every Python file the type check covers."""
    for name in SOURCE_FILES:
        yield os.path.join(ROOT, name)
    for folder in SOURCE_FOLDERS:
        for name in sorted(os.listdir(os.path.join(ROOT, folder))):
            if name.endswith(".py"):
                yield os.path.join(ROOT, folder, name)


def untyped_functions(path: str) -> List[str]:
    """Return 'file:line name (what)' for each function in the file without a return type or a parameter type."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    found = []
    where = os.path.relpath(path, ROOT).replace(os.sep, "/")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.returns is None:
            found.append(f"{where}:{node.lineno} {node.name} (return type)")
        arguments = node.args
        every = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
        every += [argument for argument in (arguments.vararg, arguments.kwarg) if argument is not None]
        for argument in every:
            if argument.annotation is None and argument.arg not in ("self", "cls"):
                found.append(f"{where}:{argument.lineno} {node.name} (parameter {argument.arg})")
    return found


class AnnotationTests(unittest.TestCase):
    def test_every_function_has_typed_parameters_and_a_return_type(self) -> None:
        missing = [item for path in python_files() for item in untyped_functions(path)]
        self.assertEqual(missing, [], "add the missing type annotations:\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
