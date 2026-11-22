# Blast Radius — safe first submission

This is a **complete self-contained submission** to [RWX Blast Radius](https://www.joinbuilders.org/buildathon/tracks/blast-radius), intended to check that your repository builds, runs, and scores correctly.

- Selects **every** test for every commit (expected zero missed failures).
- Schedules tests by longest-processing-time-first and chooses the best worker count 1–8, including two seconds per worker.
- Uses no SQLGlot source, static maps, generated indexes, sample answers, network calls, or writes at runtime.
- It is the tested **`all` baseline** that the user measured at **1104.510 simulated seconds** on the 30-commit sample (CPU time excluded). The official practice score includes CPU overhead and may differ.

## Quick local test without Docker

```bash
cd blast-radius-submission
python3 self_test.py

# The user has already extracted the official sample here:
export DATA_DIR="$HOME/blast-official/sample"
BLAST_DATA="$DATA_DIR" python3 selector.py > output.jsonl
python3 check_output.py "$DATA_DIR/tests.json" "$DATA_DIR/commits" output.jsonl "$DATA_DIR/answers.json"
```

Expected: `PASS`, `Missed commits: 0`, with simulated score similar to 1104.510 s.

## Github deployment without local Docker or sudo

1. Create a **new public and empty** GitHub repository (`blast-radius-submission`).
2. From the unpacked folder, run `git init -b main`, `git add .`, `git commit -m "Verified safe baseline"`.
3. Add your repository remote, then `git push -u origin main`.
4. Open the **Actions** tab. The automatically triggered `Verify Blast Radius submission` workflow downloads the official 30-commit sample, builds the image using Docker on a GitHub runner, runs it with competition restrictions, and scores its output.
5. If all checks pass, copy the exact SHA from `git rev-parse HEAD` and paste your public repository URL + SHA into the official [Blast Radius submission page](https://www.joinbuilders.org/buildathon/tracks/blast-radius).
6. Confirm that the platform records a **valid scored** submission. A green GitHub Actions result alone does **not** mean you submitted to Builders.

## File manifest

| File | Use |
| ---- | --- |
| `Dockerfile` | Required root-level competition image. Runs as unprivileged user. |
| `selector.py` | Actual entry point; reads `/data/tests.json` and `/data/commits/*.patch`. |
| `check_output.py` | Local output validator and approximate sample scorer; excludes CPU charge. |
| `self_test.py` | Offline smoke test. |
| `.github/workflows/verify.yml` | Docker + protocol integration test on GitHub's hosted runner. |
| `.dockerignore`, `.gitignore` | Prevent sample answer files and outputs entering image/repository. |

## Caveats

- All-tests is safe but not a winning test-selection strategy. Submit it first to confirm that packaging and official scoring work, then separately improve.
- This package intentionally **does not include** your generated `coverage_index.json.gz`. It does not use the experimental coverage selector, which missed 4 sample commits.
- The official server, not GitHub Actions, decides competition validity and scoring. All three server runs must be valid.
- Don't accidentally commit official `answers.json`, test patches, or large data artifacts.
