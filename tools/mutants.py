#!/usr/bin/env python3
"""Self-made labelled commits for checking the selector WITHOUT the sample answers.

  mutants.py gen   SQLGLOT_ROOT OUT_DIR COUNT SEED [KINDS]   write OUT_DIR/commits/*.patch
  mutants.py label SQLGLOT_ROOT TESTS_JSON OUT_DIR [JOBS]    run pytest per patch, write answers.json

KINDS is a comma list from: code, class, fixture, test (default "code").
"class" restricts code edits to statements that sit directly in a class body.
Each mutant is one small edit of the pinned checkout. `label` applies it to a
private copy, runs the whole suite the way the organisers describe, and records
which catalog tests fail. The result has the layout of the official sample, so
selector.py and score.py run on it unchanged.
"""
import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

RULES = [
    (r" is not ", " is "), (r" is (?!not )", " == "), (r" == ", " != "), (r" != ", " == "),
    (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" > ", " >= "),
    (r" <= ", " < "), (r" >= ", " > "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bnot ", ""), (r" \+ ", " - "), (r"(?<![\w.\"'])(\d+)(?![\w.\"'])", None),
    (r"(?<!for \w)(?<!for \w\w) in ", " not in "),
]


def mutate_line(line, rng):
    options = []
    for pattern, repl in RULES:
        for match in re.finditer(pattern, line):
            new = str(int(match.group(1)) + 1) if repl is None else repl
            options.append(line[:match.start()] + new + line[match.end():])
    return rng.choice(options) if options else None


def gen(root, out, count, seed, kinds):
    rng = random.Random(seed)
    os.makedirs(os.path.join(out, "commits"), exist_ok=True)
    files = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout.split()
    pools = {
        "code": [f for f in files if f.startswith("sqlglot/") and f.endswith(".py")],
        "test": [f for f in files if f.startswith("tests/") and f.endswith(".py")],
        "fixture": [f for f in files if f.startswith("tests/fixtures/") and f.endswith(".sql")],
    }
    lines_of = {}
    weighted = {k: [] for k in pools}
    for kind, names in pools.items():
        for name in names:
            text = open(os.path.join(root, name), encoding="utf-8").read()
            lines_of[name] = text.split("\n")
            weighted[kind].extend((name, i) for i in range(len(lines_of[name])))
    weighted["class"] = []
    for name in pools["code"]:
        for node in ast.walk(ast.parse("\n".join(lines_of[name]))):
            if isinstance(node, ast.ClassDef):
                for stmt in node.body:
                    if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        weighted["class"].extend((name, i) for i in range(stmt.lineno - 1, stmt.end_lineno))
    made, kinds_meta = 0, {}
    while made < count:
        kind = kinds[made % len(kinds)]
        name, i = rng.choice(weighted[kind])
        old = lines_of[name]
        line = old[i]
        if not line.strip() or line.lstrip().startswith(("#", "--")):
            continue
        if kind == "fixture":
            words = line.split(" ")
            pos = rng.randrange(len(words) + 1)
            words.insert(pos, rng.choice(["42", "x", "NULL", "0"]))
            new_line = " ".join(words)
        else:
            new_line = mutate_line(line, rng)
            if new_line is None:
                continue
        new = old[:i] + [new_line] + old[i + 1:]
        if kind != "fixture":
            try:
                if ast.dump(ast.parse("\n".join(new))) == ast.dump(ast.parse("\n".join(old))):
                    continue
            except SyntaxError:
                continue
        diff = "".join(difflib.unified_diff(
            [l + "\n" for l in old[:-1]] + ([old[-1]] if old[-1] else []),
            [l + "\n" for l in new[:-1]] + ([new[-1]] if new[-1] else []),
            "a/" + name, "b/" + name, n=3))
        cid = "m%s%04d" % (kind[0], made + seed * 10000)
        with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8") as handle:
            handle.write("diff --git a/%s b/%s\n" % (name, name) + diff)
        kinds_meta[cid] = {"kind": kind, "file": name, "line": i + 1}
        made += 1
    json.dump(kinds_meta, open(os.path.join(out, "kinds.json"), "w"), indent=0)


def label(root, tests_json, out, jobs):
    here = os.path.dirname(os.path.abspath(__file__))
    tests = json.load(open(tests_json))["tests"]
    number = {t["id"]: t["number"] for t in tests}
    by_file = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    kinds = json.load(open(os.path.join(out, "kinds.json")))
    scratch = os.environ.get("BLAST_SCRATCH", "/dev/shm/blast_mut_%d" % os.getpid())
    results = os.path.join(out, "results")
    os.makedirs(results, exist_ok=True)
    cpus = sorted(os.sched_getaffinity(0))
    per = 4

    def run(job):
        slot, cid = job
        done = os.path.join(results, cid + ".json")
        if os.path.exists(done):
            return
        copy = os.path.join(scratch, cid)
        shutil.rmtree(copy, ignore_errors=True)
        os.makedirs(copy)
        for item in ("sqlglot", "tests", "sqlglotc", "setup.py", "setup.cfg", "pyproject.toml"):
            src = os.path.join(root, item)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(copy, item), ignore=shutil.ignore_patterns("__pycache__"))
            elif os.path.exists(src):
                shutil.copy(src, copy)
        patch = os.path.abspath(os.path.join(out, "commits", cid + ".patch"))
        applied = subprocess.run(["patch", "-p1", "-s", "-i", patch], cwd=copy, capture_output=True, text=True)
        if applied.returncode:
            raise RuntimeError("patch failed for %s: %s" % (cid, applied.stdout + applied.stderr))
        report = os.path.join(copy, "report.json")
        mine = cpus[(slot * per) % len(cpus):][:per]
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1", BLAST_REPORT=report,
                   PYTHONPATH=copy + os.pathsep + here,
                   PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
        try:
            subprocess.run(["taskset", "-c", ",".join(map(str, mine)), sys.executable, "-m", "pytest",
                            "-p", "blast_report", "-p", "no:cacheprovider", "-q", "-n", str(per),
                            "--timeout", "300", "tests"],
                           cwd=copy, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1500)
            data = json.load(open(report))
        except Exception as exc:  # noqa: BLE001
            data = {"error": repr(exc)}
        failing = set()
        if "error" not in data:
            for node in data["failed"]:
                if node in number:
                    failing.add(number[node])
                elif node in by_file:
                    failing.update(by_file[node])
            for node in data["broken"]:
                failing.update(by_file.get(node, []))
                if node not in by_file:
                    data.setdefault("odd", []).append(node)
        data["failing"] = sorted(failing)
        json.dump(data, open(done, "w"))
        shutil.rmtree(copy, ignore_errors=True)

    todo = sorted(kinds)
    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, [(i % jobs, cid) for i, cid in enumerate(todo)]))
    answers, errors = {}, []
    for cid in todo:
        data = json.load(open(os.path.join(results, cid + ".json")))
        if "error" in data or data.get("odd"):
            errors.append((cid, data.get("error"), data.get("odd")))
        answers[cid] = {"failing": data["failing"], "kind": kinds[cid]["kind"]}
    json.dump({"commits": answers}, open(os.path.join(out, "answers.json"), "w"))
    shutil.copy(tests_json, os.path.join(out, "tests.json"))
    shutil.rmtree(scratch, ignore_errors=True)
    killed = sum(1 for a in answers.values() if a["failing"])
    print("labelled %d mutants, %d break something, %d errors %s" % (len(answers), killed, len(errors), errors[:5]),
          file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "gen":
        kinds = sys.argv[6].split(",") if len(sys.argv) > 6 else ["code"]
        gen(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), kinds)
    else:
        label(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]) if len(sys.argv) > 5 else 12)
