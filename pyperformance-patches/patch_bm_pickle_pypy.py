"""
Patch pyperformance bm_pickle for PyPy compatibility.

PyPy provides a _pickle accelerator module, so the existing IS_PYPY check
in the pure-python guard incorrectly raises RuntimeError.  Move IS_PYPY to
the accelerated-module check instead so PyPy can use its C extension.

Source: https://github.com/python/pyperformance/pull/461
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_pickle/run_benchmark.py",
    "./*/lib/*/site-packages/benchmarks/bm_pickle/run_benchmark.py",
    "./*/*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_pickle/run_benchmark.py",
    "./*/*/lib/*/site-packages/benchmarks/bm_pickle/run_benchmark.py",
]

# Deduplicate by real path in case any entries are symlinks to the same file
seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: bm_pickle run_benchmark.py not found – patch failed")
    sys.exit(1)

OLD_PURE = "if not (options.pure_python or IS_PYPY):"
NEW_PURE = "if not (options.pure_python):"
OLD_ACCEL = "if not is_accelerated_module(pickle):"
NEW_ACCEL = "if not is_accelerated_module(pickle) and not IS_PYPY:"

failed = False
for path in files:
    txt = open(path).read()
    original = txt

    if OLD_PURE in txt:
        txt = txt.replace(OLD_PURE, NEW_PURE)
    elif NEW_PURE in txt:
        print(f"NOTE: {path} pure_python guard already patched")
    else:
        print(f"ERROR: {path} pure_python guard pattern not found – version mismatch?")
        failed = True

    if OLD_ACCEL in txt:
        txt = txt.replace(OLD_ACCEL, NEW_ACCEL)
    elif NEW_ACCEL in txt:
        print(f"NOTE: {path} accelerated-module guard already patched")
    else:
        print(f"ERROR: {path} accelerated-module guard pattern not found – version mismatch?")
        failed = True

    if txt != original:
        open(path, "w").write(txt)
        print(f"Patched {path}")

if failed:
    sys.exit(1)
