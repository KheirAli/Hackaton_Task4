# Blast Radius — execution-evidence test selection

Submission for [RWX Blast Radius](https://www.joinbuilders.org/buildathon/tracks/blast-radius).
For every patch to sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` it prints which
tests to run and how to spread them over at most 8 workers.

## How it decides

Everything rests on one map of the **pinned commit only**, `blast_index.zip`, built by `tools/`.
Each of the 1,620 catalog tests was run **alone, in a fresh process tree** (worker processes of
`multiprocessing` pools and `python -c` children included) and we recorded:

| Evidence | Meaning |
| --- | --- |
| function lines | lines the test ran inside functions and lambdas (call time) |
| imports | files the test's processes imported, and the lines that ran at import time |
| opened files | repository files it opened: SQL fixtures, CSV/JSON data, sources read as text |
| used classes | classes on whose instances it ran a method (with their base classes) |

A second pass records function lines per test while the whole suite shares one process, to
catch tests that reach code only through state left by earlier tests. The two are unioned.

At run time `selector.py` (standard library only) handles each changed file of a patch:

* **Python file.** Apply the patch to the base text, parse both versions and compare the ASTs.
  * Same AST (comments, blank lines, formatting): nothing can change. Tests that *read the
    file as text* are still selected.
  * Difference inside a function or lambda body: tests that ran the lines of the enclosing
    statement. A changed signature selects the tests that ran the function. Added statements
    select the tests that ran the enclosing function.
  * Difference in module- or class-level code: every test that imported the file. sqlglot loads
    dialects lazily, so this is usually a small set. For files that (nearly) every test imports
    it would mean "run everything", so a change strictly inside one top-level class is narrowed
    to the tests that *used* that class or mentioned it by name in a line they ran.
  * A new version that does not parse: every test that touches the file.
  * Only a comparison operator changed, within one of the pairs `is`/`==`, `is not`/`!=`,
    `<`/`<=`, `>`/`>=`: a separate trace pass evaluated both operators on the real operands
    every time a test reached the comparison. Tests for which the two always agreed cannot
    behave differently and are dropped; tests without a profile for the site are kept.
* **Any other file** (fixtures, data): the tests that opened it; if none did, the tests that
  opened something in the same directory.
* **Anything unexplained** — unknown files, new Python files, suite configuration, patches that
  do not apply, internal errors — falls back to a wider set, in the end **every test**. Tests
  the index has never seen are kept in every non-empty selection.

Selected tests are packed with longest-first greedy scheduling for each worker count 1–8,
keeping the cheapest under `max load + 2 s × workers`.

No patch, kind of edit or answer is looked up anywhere; the selector never sees
`sample/answers.json`.

## Results

Measured locally with the organisers' `score.py` (simulated seconds, CPU time not included;
the selector itself uses about 0.2 s of CPU for the 30 sample commits):

| Selector | 30-commit sample | Misses |
| --- | ---: | ---: |
| every test, 3 workers (starter) | 1,614.1 s | 0 |
| every test, best worker count | 1,104.5 s | 0 |
| this selector, class narrowing and comparison profile off | 214.4 s | 0 |
| this selector, comparison profile off (`BLAST_CMP_PROFILE=0`) | 179.7 s | 0 |
| this selector (default) | 126.9 s | 0 |

Checked on our own mutated commits (one-line edits, labelled by running the full suite on each):

| Mutant set | Labelled | Break something | Misses | Mean cost per commit |
| --- | ---: | ---: | ---: | ---: |
| random: code, fixture and test edits | 237 | 183 | 0 | 9.5 s |
| class-body statements only | 200 | 126 | 0 | 13.1 s |
| comparison operator swapped for any other | 89 | 64 | 0 | 11.5 s |
| comparison operator swapped within a stable pair | 115 | 25 | 0 | 3.5 s |

What the mutants caught along the way:

* CPython reports no line event for a function's own first line, so one-line lambdas looked
  unused and one commit was missed. The tracer now records function entry as well.
* The comparison profile was first applied to every operator swap. A swap such as `==` to `<=`
  on hash values gives a different outcome from run to run, and one commit was missed. It is
  now limited to `is`/`==`, `is not`/`!=`, `<`/`<=` and `>`/`>=`.
* Nine labels were wrong: the 28 s `test_max_nodes` failed under machine load during labelling
  and passes when re-run alone. `tools/mutants.py` now re-runs slow failures before recording them.

## Reproduce

```bash
python3 self_test.py                               # protocol, patch handling, fallbacks

export KIT=$HOME/blast-official                    # unpacked starter kit (score.py, sample/)
BLAST_DATA=$KIT/sample python3 selector.py > output.jsonl
python3 $KIT/score.py $KIT/sample $KIT/sample/answers.json output.jsonl
BLAST_DEBUG=1 BLAST_DATA=$KIT/sample python3 selector.py 2>&1 >/dev/null | less   # reasons
```

Rebuilding the index (Python 3.13 with the packages of the kit's `requirements.lock`, a clean
sqlglot checkout at the pinned commit; about 15 minutes on a large machine):

```bash
python tools/trace_all.py  $SQLGLOT $KIT/sample/tests.json raw_a      # one fresh process tree per test
python tools/trace_all.py  $SQLGLOT $KIT/sample/tests.json raw_b      # again: other hash seed
(cd $SQLGLOT && SKIP_INTEGRATION=0 BLAST_TRACE_ROOT=$SQLGLOT BLAST_SUITE_RAW=$PWD/raw_suite \
   BLAST_TESTS_JSON=$KIT/sample/tests.json PYTHONPATH=$SQLGLOT:/path/to/tools \
   python -m pytest -p suite_trace -p no:cacheprovider tests)        # whole suite, one process
python tools/class_info.py $SQLGLOT > class_info.json
BLAST_TRACE_CMP=1 python tools/trace_all.py $SQLGLOT $KIT/sample/tests.json raw_cmp   # comparison profile
python tools/build_index.py $SQLGLOT $KIT/sample/tests.json raw_a,raw_b,raw_suite blast_index.zip class_info.json raw_cmp
```

Checking for missed failures without the sample answers (this is how the rules above were
validated):

```bash
python tools/mutants.py gen   $SQLGLOT mut 240 1 code,code,code,class,fixture,test
python tools/mutants.py label $SQLGLOT $KIT/sample/tests.json mut    # full pytest run per mutant
BLAST_DATA=mut python3 selector.py > mut/out.jsonl
python3 $KIT/score.py mut mut/answers.json mut/out.jsonl
```

## Files

| File | Use |
| --- | --- |
| `Dockerfile` | The image the scorer builds: official base image, `selector.py`, `blast_index.zip`. No network needed beyond the base image. |
| `selector.py` | Entry point. Reads `/data/tests.json` and `/data/commits/*.patch`, prints one JSON line per commit. |
| `blast_index.zip` | Map of the pinned commit: coverage, imports, opened files, class users, base sources. |
| `tools/tracer/sitecustomize.py` | Tracer loaded into every Python process of a traced test (`sys.monitoring`, audit hook). |
| `tools/trace_all.py`, `tools/run_one.py` | Run every catalog test alone and collect its trace. |
| `tools/suite_trace.py` | pytest plugin: per-test function lines with the suite in one process. |
| `tools/class_info.py` | Lists classes whose construction the tracer can observe. |
| `tools/build_index.py` | Turns traces into `blast_index.zip`. |
| `tools/mutants.py`, `tools/blast_report.py` | Make and label our own mutated commits to measure misses. |
| `self_test.py`, `check_output.py` | Offline tests and an output validator. |
| `.github/workflows/verify.yml` | Builds the image on GitHub, runs it under the scorer's limits, scores it with the kit's `score.py`. |

## Known limits

* Coverage is evidence from the clean commit. A change that makes a test reach code it never
  reached before, through data rather than through a line the test ran, can be missed.
* The class narrowing is a heuristic: it assumes class-level data is read through instances of
  the class or by naming it. It is only applied to files that nearly every test imports.
* Tests that take 20–30 s (`test_max_nodes`, `test_execute_tpcds`) cover much of the core, so
  commits that touch widely used code still cost about 25 s.
