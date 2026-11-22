#!/usr/bin/env python3
"""Validate contestant stdout against official JSON protocol and score the sample.

Usage: python3 check_output.py PATH_TO_TESTS_JSON PATH_TO_PATCH_DIR PATH_TO_OUTPUT_JSONL [ANSWERS_JSON]
Only scores missed failures if optional sample answer JSON is provided.
"""
import json
import sys
from pathlib import Path


def main():
    if len(sys.argv) not in (4, 5):
        raise SystemExit("Usage: check_output.py TESTS_JSON COMMITS_DIR OUTPUT_JSONL [ANSWERS_JSON]")
    tests = json.loads(Path(sys.argv[1]).read_text())["tests"]
    known = {int(x["number"]): int(round(float(x["seconds"]) * 1000)) for x in tests}
    patches = {p.stem for p in Path(sys.argv[2]).glob("*.patch")}
    seen = set()
    total_ms = 0
    selected = {}
    raw = Path(sys.argv[3]).read_text(encoding="utf-8-sig")
    if raw.startswith("\ufeff"):
        raise ValueError("Invalid BOM")
    if len(raw.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Output exceeds 2 MiB")
    for lineno, line in enumerate(raw.splitlines(), 1):
        obj = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Nonstandard JSON")))
        if not isinstance(obj, dict) or set(obj) != {"commit", "workers"}:
            raise ValueError("Bad shape line {}".format(lineno))
        cid = obj["commit"]
        workers = obj["workers"]
        if not isinstance(cid, str) or cid not in patches or cid in seen:
            raise ValueError("Unknown or repeated commit line {}".format(lineno))
        if not isinstance(workers, list):
            raise ValueError("Workers is not a list")
        seen.add(cid)
        ids = set()
        loads = []
        for worker in workers:
            if not isinstance(worker, list):
                raise ValueError("Bad worker")
            load = 0
            for value in worker:
                if type(value) != int or value not in known or value in ids:
                    raise ValueError("Bad/duplicate test {} line {}".format(value, lineno))
                ids.add(value)
                load += known[value]
            if worker:
                loads.append(load)
        if len(loads) > 8:
            raise ValueError("Too many workers")
        total_ms += (max(loads) + 2000 * len(loads)) if loads else 0
        selected[cid] = ids
    if seen != patches:
        raise ValueError("Missing commits: {}".format(patches - seen))
    misses = None
    if len(sys.argv) == 5:
        answers = json.loads(Path(sys.argv[4]).read_text())["commits"]
        misses = sum(not set(v["failing"]).issubset(selected.get(k, set())) for k, v in answers.items())
        total_ms += 300000 * misses
    print("PASS: {} commits, {} recorded tests".format(len(seen), len(known)))
    print("Simulated time: {:.3f} s{}".format(total_ms / 1000, " (not including CPU)"))
    if misses is not None:
        print("Missed commits: {}".format(misses))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("FAIL: {}".format(exc), file=sys.stderr)
        sys.exit(1)
