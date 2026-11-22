"""pytest plugin: per-test function-line coverage while the WHOLE suite shares a process.

    BLAST_TRACE_ROOT=<checkout> BLAST_SUITE_RAW=<dir> BLAST_TESTS_JSON=<tests.json> \
        python -m pytest -p suite_trace -p no:cacheprovider tests

tools/trace_all.py runs every test alone, which is the right view of what a
test needs. In a shared process a test can also run lines only because of
state left by earlier tests (dialects already loaded, warm caches). This plugin
records that second view; build_index.py unions both. Output has the same
layout as trace_all.py (one <number>.json per catalog test).
"""
import json
import os
import sys

root = os.path.realpath(os.environ["BLAST_TRACE_ROOT"]) + os.sep
out_dir = os.environ["BLAST_SUITE_RAW"]
numbers = {t["id"]: t["number"] for t in json.load(open(os.environ["BLAST_TESTS_JSON"]))["tests"]}
mon = sys.monitoring
TOOL = mon.COVERAGE_ID
codes = {}
current = None


def on_line(code, line):
    key = codes.get(code)
    if key is None:
        name = code.co_filename
        key = name[len(root):] if name.startswith(root) and code.co_flags & 1 else False
        codes[code] = key
    if key and current is not None:
        current.add((key, line))
    return mon.DISABLE


def on_start(code, offset):
    # LINE is not reported for a function's own first line (one-line lambdas).
    name = code.co_filename
    if current is not None and code.co_flags & 1 and name.startswith(root):
        rel = name[len(root):]
        current.add((rel, code.co_firstlineno))
        for _, _, line in code.co_lines():
            if line:
                current.add((rel, line))
                break
    return mon.DISABLE


def pytest_configure(config):
    os.makedirs(out_dir, exist_ok=True)
    mon.use_tool_id(TOOL, "blast-suite")
    mon.register_callback(TOOL, mon.events.LINE, on_line)
    mon.register_callback(TOOL, mon.events.PY_START, on_start)
    mon.set_events(TOOL, mon.events.LINE | mon.events.PY_START)


def pytest_runtest_logstart(nodeid, location):
    global current
    current = set()
    mon.restart_events()


def pytest_runtest_logfinish(nodeid, location):
    global current
    lines, current = current, None
    number = numbers.get(nodeid)
    if number is None or lines is None:
        return
    fn = {}
    for name, line in lines:
        fn.setdefault(name, []).append(line)
    with open(os.path.join(out_dir, "%d.json" % number), "w") as handle:
        json.dump({"id": nodeid, "outcome": "passed", "detail": "", "imp": {}, "fn": fn, "opens": [],
                   "classes": [], "procs": 1}, handle, separators=(",", ":"))
