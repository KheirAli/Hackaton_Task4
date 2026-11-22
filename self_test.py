#!/usr/bin/env python3
"""Offline checks of selector.py: output protocol, patch handling, fallbacks.

Needs only the standard library and blast_index.zip. Uses a synthetic catalog
(real test ids, made-up durations) and hand-written patches against the pinned
sqlglot commit; it never touches the organisers' sample answers.
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parent
index = zipfile.ZipFile(root / "blast_index.zip")
meta = json.loads(index.read("meta.json"))
ids = meta["tests"]


def src(name):
    return index.read("src/" + name).decode().split("\n")


def one_line_patch(name, lineno, new_line=None, insert=None):
    """Unified diff replacing (or inserting before) one line of a base file."""
    lines = src(name)
    lo, hi = max(1, lineno - 3), min(len(lines), lineno + 3)
    body, old_count, new_count = [], 0, 0
    for n in range(lo, hi + 1):
        text = lines[n - 1]
        if n == lineno and insert is not None:
            body.append("+" + insert)
            new_count += 1
        if n == lineno and new_line is not None:
            body += ["-" + text, "+" + new_line]
        else:
            body.append(" " + text)
        old_count += 1
        new_count += 1
    return "\n".join(["diff --git a/%s b/%s" % (name, name), "--- a/" + name, "+++ b/" + name,
                      "@@ -%d,%d +%d,%d @@" % (lo, old_count, lo, new_count)] + body) + "\n"


def find(name, needle):
    for n, text in enumerate(src(name), 1):
        if needle in text:
            return n, text
    raise AssertionError("pinned source changed: %r not in %s" % (needle, name))


n1, t1 = find("sqlglot/serde.py", "if node.type and node.type is not node:")
n2, t2 = find("sqlglot/transforms.py", "def eliminate_join_marks(")
patches = {
    "code": one_line_patch("sqlglot/serde.py", n1, t1.replace(" and ", " or ")),
    "comment": one_line_patch("sqlglot/transforms.py", n2 + 1, insert="    # only a comment"),
    "syntax": one_line_patch("sqlglot/serde.py", n1, t1 + " ("),
    "unknown": "diff --git a/conftest.py b/conftest.py\nnew file mode 100644\n--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+x = 1\n",
    "garbage": "this is not a patch\n",
    "empty": "",
    "docs": "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1,1 +1,1 @@\n-x\n+y\n",
}
patches["fixture"] = (
    "diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify.sql\n"
    "--- a/tests/fixtures/optimizer/simplify.sql\n+++ b/tests/fixtures/optimizer/simplify.sql\n"
    "@@ -1,1 +1,1 @@\n-x\n+y\n"
)

with tempfile.TemporaryDirectory() as tmp:
    data = Path(tmp)
    (data / "commits").mkdir()
    tests = [{"number": i, "id": t, "file": t.split("::")[0], "seconds": round(0.01 + (i % 7) * 0.013, 3)}
             for i, t in enumerate(ids)]
    tests.append({"number": len(ids), "id": "tests/test_new.py::TestNew::test_unknown", "file": "tests/test_new.py",
                  "seconds": 0.5})
    (data / "tests.json").write_text(json.dumps({"tests": tests}))
    for cid, text in patches.items():
        (data / "commits" / (cid + ".patch")).write_text(text)
    run = subprocess.run([sys.executable, str(root / "selector.py")], env={**os.environ, "BLAST_DATA": str(data)},
                         capture_output=True, text=True, check=True)
    assert run.stdout.endswith("\n")
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    assert sorted(r["commit"] for r in rows) == sorted(patches), "one line per commit"
    picked = {}
    for row in rows:
        assert set(row) == {"commit", "workers"}
        flat = [n for worker in row["workers"] for n in worker]
        assert all(type(n) is int and 0 <= n < len(tests) for n in flat)
        assert len(flat) == len(set(flat)), "no test twice"
        assert all(row["workers"]) and len(row["workers"]) <= 8
        picked[row["commit"]] = set(flat)
    everything = set(range(len(tests)))
    by_id = {t["id"]: t["number"] for t in tests}
    assert picked["comment"] == set(), "a comment changes nothing"
    assert picked["empty"] == set() and picked["docs"] == set()
    assert picked["unknown"] == everything and picked["garbage"] == everything, "unexplained change runs everything"
    assert by_id["tests/test_serde.py::TestSerde::test_serde"] in picked["code"]
    assert 0 < len(picked["code"]) < 200, len(picked["code"])
    assert len(ids) in picked["code"], "a test the index has never seen is always kept"
    assert picked["code"] <= picked["syntax"], "a file that no longer parses breaks all its importers"
    assert by_id["tests/test_optimizer.py::TestOptimizer::test_simplify"] in picked["fixture"]
    assert len(picked["fixture"]) < 10
    check = subprocess.run([sys.executable, str(root / "check_output.py"), str(data / "tests.json"),
                            str(data / "commits"), "/dev/stdin"], input=run.stdout, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    # A missing index must still give a valid all-tests answer.
    run = subprocess.run([sys.executable, str(root / "selector.py")],
                         env={**os.environ, "BLAST_DATA": str(data), "BLAST_INDEX": str(data / "nope.zip")},
                         capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(rows) == len(patches)
    assert all({n for w in r["workers"] for n in w} == everything for r in rows)
print("PASS: selector protocol, patch handling and fallbacks")
