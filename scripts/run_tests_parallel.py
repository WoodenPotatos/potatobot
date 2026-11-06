#!/usr/bin/env python3
"""Run the suite one test module per process, several at a time.

For iteration only. **The gate stays `python -m unittest discover -s tests`**
(CI and `publish_public.run_built_tree_suite`): a module run in its own process
cannot meet another module's leftovers, and "passes alone, fails in the full
suite" is a failure family this project has had (`tests/test_cog_loading.py`
unloading extensions under a module-level import). Run the serial suite before
handing anything off.

Each module runs exactly as discovery runs it (`discover -s tests -p <file>`),
so `import pg_harness` resolves the same way, and each process gets its own
PostgreSQL database because `pg_harness` names it by pid. A module that ran no
test is a failure, never a pass: a test that skips itself is a test that does
not exist, and a module that silently collects nothing is the same thing.

    venv/bin/python scripts/run_tests_parallel.py            # every module
    venv/bin/python scripts/run_tests_parallel.py -j 4 gacha  # names containing "gacha"
"""

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
RAN = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
SKIPPED = re.compile(r"skipped=(\d+)")


def run_module(path: Path) -> dict:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", path.name],
        cwd=ROOT, capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    ran = RAN.search(output)
    tail = output.rstrip().rsplit("\n", 1)[-1]
    return {
        "module": path.stem,
        "seconds": time.monotonic() - started,
        "returncode": proc.returncode,
        "tests": int(ran.group(1)) if ran else 0,
        "skipped": sum(int(n) for n in SKIPPED.findall(tail)),
        "output": output,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                        help="modules to run at once (default: CPU count)")
    parser.add_argument("names", nargs="*",
                        help="run only modules whose file name contains one of these")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    modules = sorted(TESTS.glob("test_*.py"))
    if args.names:
        modules = [m for m in modules if any(n in m.name for n in args.names)]
    if not modules:
        print("No test module matched.", file=sys.stderr)
        return 2
    # Largest first: file size is a fair proxy for run time, and starting the
    # long modules early keeps one of them from being the tail of the run.
    modules.sort(key=lambda m: m.stat().st_size, reverse=True)

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_module, m) for m in modules]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            ok = result["returncode"] == 0 and result["tests"] > 0
            print(f"{'ok  ' if ok else 'FAIL'} {result['module']:<40} "
                  f"{result['tests']:>5} tests {result['seconds']:7.1f} s", flush=True)
    wall = time.monotonic() - started

    failed = [r for r in results if r["returncode"] != 0 or r["tests"] == 0]
    for result in sorted(failed, key=lambda r: r["module"]):
        print(f"\n===== {result['module']} (exit {result['returncode']}, "
              f"{result['tests']} tests) =====")
        print(result["output"].rstrip())

    total = sum(r["tests"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    slowest = sorted(results, key=lambda r: r["seconds"], reverse=True)[:5]
    print(f"\n{len(results)} modules, {total} tests, {skipped} skipped, "
          f"{len(failed)} failed module(s), {wall:.1f} s wall with {args.jobs} jobs")
    print("Slowest: " + ", ".join(f"{r['module']} {r['seconds']:.0f} s" for r in slowest))
    print("FAILED" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
