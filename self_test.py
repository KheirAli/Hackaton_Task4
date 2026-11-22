#!/usr/bin/env python3
"""Zero-dependency smoke test for shape, coverage, and duplicate suppression."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as d:
    data = Path(d)
    (data / 'commits').mkdir()
    tests = [
        {'number': 0, 'id': 'tests/test_a.py::test_a', 'file': 'tests/test_a.py', 'seconds': 0.017},
        {'number': 1, 'id': 'tests/test_b.py::test_b', 'file': 'tests/test_b.py', 'seconds': 0.089},
        {'number': 2, 'id': 'tests/test_c.py::test_c', 'file': 'tests/test_c.py', 'seconds': 0.021},
    ]
    (data / 'tests.json').write_text(json.dumps({'tests': tests}))
    for cid in ('aaa', 'bbb'):
        (data / 'commits' / f'{cid}.patch').write_text('diff --git a/a b/a\n')
    out = data / 'output.jsonl'
    env = {**os.environ, 'BLAST_DATA': str(data)}
    run = subprocess.run([sys.executable, str(root / 'selector.py')], env=env, capture_output=True, text=True, check=True)
    assert not run.stderr, f'unexpected stderr: {run.stderr}'
    out.write_text(run.stdout)
    objects = [json.loads(line) for line in run.stdout.splitlines()]
    assert [x['commit'] for x in objects] == ['aaa', 'bbb']
    for item in objects:
        seen = [t for worker in item['workers'] for t in worker]
        assert len(item['workers']) <= 8
        assert len(seen) == len(set(seen)) == len(tests)
        assert set(seen) == {0, 1, 2}
    valid = subprocess.run([sys.executable, str(root / 'check_output.py'), str(data / 'tests.json'), str(data / 'commits'), str(out)], capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
print('PASS: offline selector and JSON protocol tests')
