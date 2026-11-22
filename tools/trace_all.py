#!/usr/bin/env python3
"""Trace every catalog test, each in its own fresh process tree.

Usage: trace_all.py SQLGLOT_ROOT TESTS_JSON RAW_DIR [JOBS]
Writes RAW_DIR/<number>.json = {id, outcome, detail, imp, fn, opens} where the
line sets are the union over the test process and all its child processes.
"""
import glob
import json
import os
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

root, tests_json, raw = sys.argv[1:4]
root = os.path.realpath(root)
raw = os.path.abspath(raw)
cpus = sorted(os.sched_getaffinity(0))
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else max(1, len(cpus) // 2 - 2)
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
os.makedirs(raw, exist_ok=True)
slots = queue.Queue()
for i in range(jobs):
    slots.put((cpus[2 * i], cpus[2 * i + 1]))


def run(test):
    out = os.path.join(raw, f"{test['number']}.json")
    if os.path.exists(out):
        return
    prefix = os.path.join(raw, f"part{test['number']}")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
               BLAST_TRACE_OUT=prefix, BLAST_TRACE_ROOT=root,
               PYTHONPATH=os.path.join(here, "tracer"),
               PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))
    pair = slots.get()
    try:
        subprocess.run(["taskset", "-c", f"{pair[0]},{pair[1]}", sys.executable,
                        os.path.join(here, "run_one.py"), root, test["id"], prefix + ".outcome"],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
    finally:
        slots.put(pair)
    record = {"id": test["id"], "outcome": "error", "detail": "", "imp": {}, "fn": {}, "opens": [], "classes": [], "lists": [], "procs": 0}
    try:
        text = open(prefix + ".outcome").read()
        record["outcome"], _, record["detail"] = text.partition("\n")
        os.remove(prefix + ".outcome")
    except OSError:
        pass
    opens, classes, lists = set(), set(), set()
    for part in glob.glob(prefix + ".*.json"):
        data = json.load(open(part))
        record["procs"] += 1
        opens.update(data["opens"])
        classes.update(data.get("classes", ()))
        lists.update(data.get("lists", ()))
        for key in ("imp", "fn"):
            for name, lines in data[key].items():
                record[key].setdefault(name, set()).update(lines)
        for name, sites in data.get("cmp", {}).items():
            mine = record.setdefault("cmp", {}).setdefault(name, {})
            for site, state in sites.items():
                mine[site] = mine.get(site, 0) | state
        os.remove(part)
    record["opens"] = sorted(opens)
    record["classes"] = sorted(classes)
    record["lists"] = sorted(lists)
    for key in ("imp", "fn"):
        record[key] = {name: sorted(lines) for name, lines in record[key].items()}
    with open(out + ".tmp", "w") as handle:
        json.dump(record, handle, separators=(",", ":"))
    os.replace(out + ".tmp", out)


with ThreadPoolExecutor(jobs) as pool:
    list(pool.map(run, tests))
# A child process can outlive its test (pool shutdown) and write its trace after
# the merge above. Fold such late parts into the test's record.
import re  # noqa: E402

for part in sorted(glob.glob(os.path.join(raw, "part*.*.json"))):
    number = re.match(r"part(\d+)\.", os.path.basename(part)).group(1)
    target = os.path.join(raw, number + ".json")
    record, data = json.load(open(target)), json.load(open(part))
    record["procs"] += 1
    record["opens"] = sorted(set(record["opens"]) | set(data["opens"]))
    record["classes"] = sorted(set(record.get("classes", ())) | set(data.get("classes", ())))
    record["lists"] = sorted(set(record.get("lists", ())) | set(data.get("lists", ())))
    for key in ("imp", "fn"):
        for name, lines in data[key].items():
            record[key][name] = sorted(set(record[key].get(name, ())) | set(lines))
    for name, sites in data.get("cmp", {}).items():
        mine = record.setdefault("cmp", {}).setdefault(name, {})
        for site, state in sites.items():
            mine[site] = mine.get(site, 0) | state
    json.dump(record, open(target, "w"), separators=(",", ":"))
    os.remove(part)
    print("merged late part for test", number, file=sys.stderr)
outcomes = {}
for test in tests:
    o = json.load(open(os.path.join(raw, f"{test['number']}.json")))["outcome"]
    outcomes[o] = outcomes.get(o, 0) + 1
print("outcomes:", outcomes, file=sys.stderr)
