#!/usr/bin/env python3
"""RWX Blast Radius submission: choose all tests, optimize worker allocation.

Input: /data/tests.json and /data/commits/*.patch
Output: one strict JSON line per commit, and NOTHING else on stdout.
No external dependencies, network, or writable files at runtime.
"""
import json
import os
import sys
from pathlib import Path

DATA = Path(os.environ.get('BLAST_DATA', '/data'))
MAX_WORKERS = 8
WORKER_OVERHEAD_MS = 2000


def schedule(tests):
    """Largest-processing-time-first assignment, best of 1..8 workers.

    Match the official integer-millisecond runtime accounting.
    """
    if not tests:
        return []
    ranked = sorted(
        ((int(t['number']), round(float(t['seconds']) * 1000)) for t in tests),
        key=lambda item: (-item[1], item[0]),
    )
    best_workers = None
    best_cost = None
    for k in range(1, min(MAX_WORKERS, len(ranked)) + 1):
        workers = [[] for _ in range(k)]
        loads = [0] * k
        for number, ms in ranked:
            worker = min(range(k), key=lambda i: (loads[i], i))
            workers[worker].append(number)
            loads[worker] += ms
        nonempty = [w for w in workers if w]
        cost = max(loads) + WORKER_OVERHEAD_MS * len(nonempty)
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_workers = nonempty
    return best_workers


def main():
    try:
        catalog = json.loads((DATA / 'tests.json').read_text(encoding='utf-8'))
        tests = catalog['tests']
        if not isinstance(tests, list):
            raise ValueError('tests.json field tests must be a list')
        commits_dir = DATA / 'commits'
        patch_files = sorted(commits_dir.glob('*.patch'))
        if not patch_files:
            raise ValueError('no .patch files found in /data/commits')
        # Every commit stands alone; same test catalog means same safe selection.
        assignment = schedule(tests)
        for patch in patch_files:
            line = {'commit': patch.stem, 'workers': assignment}
            print(json.dumps(line, separators=(',', ':'), allow_nan=False))
    except Exception as exc:
        print(f'Blast Radius selector error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
