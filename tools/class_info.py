#!/usr/bin/env python3
"""List repository classes whose construction is observable by the tracer.

Usage: class_info.py SQLGLOT_ROOT  ->  JSON list of "file:QualName" on stdout

A class qualifies when some class in its MRO defines __init__ or __new__ in a
repository file: creating an instance then runs repository code with `self`,
which tools/tracer records. For other classes (plain Enums, for example) the
tracer cannot tell who uses them, so the selector never narrows on them.
"""
import importlib
import json
import os
import pkgutil
import sys

root = os.path.realpath(sys.argv[1]) + os.sep
sys.path.insert(0, root[:-1])
import sqlglot  # noqa: E402

for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name == "sqlglot.__main__":
        continue
    try:
        importlib.import_module(info.name)
    except Exception:
        pass


def repo_file(klass):
    module = sys.modules.get(getattr(klass, "__module__", None))
    path = getattr(module, "__file__", None) or ""
    return path[len(root):] if path.startswith(root) else None


def in_repo(func):
    func = getattr(func, "__func__", func)
    code = getattr(func, "__code__", None)
    return code is not None and code.co_filename.startswith(root)


seen, out = set(), set()


def visit(klass):
    if klass in seen:
        return
    seen.add(klass)
    path = repo_file(klass)
    if path is None or "<locals>" in klass.__qualname__:
        return
    for base in klass.__mro__:
        if any(in_repo(vars(base).get(name)) for name in ("__init__", "__new__")):
            out.add(path + ":" + klass.__qualname__)
            break
    for value in list(vars(klass).values()):
        if isinstance(value, type) and value.__qualname__.startswith(klass.__qualname__ + "."):
            visit(value)


for module in list(sys.modules.values()):
    path = getattr(module, "__file__", None) or ""
    if not path.startswith(root):
        continue
    for value in list(vars(module).values()):
        if isinstance(value, type) and value.__module__ == module.__name__:
            visit(value)
json.dump(sorted(out), sys.stdout)
