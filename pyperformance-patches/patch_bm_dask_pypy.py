"""
Patch pyperformance bm_dask for PyPy compatibility.

dask.sizeof falls back to sys.getsizeof() for any type it has no
registration for, and PyPy's sys.getsizeof() raises TypeError unless a
default is passed.  distributed.sizeof.safe_sizeof() swallows the error, but
logs a warning plus PyPy's very long explanation of why getsizeof cannot
work - for every object it sizes - which floods stderr and slows the
benchmark down.  Supply a default so the fallback returns a number instead.

(The BufferError that killed this benchmark is in tornado, and is handled by
patch_tornado_iostream_pypy.py.)

dask lives in the benchmark venv, which pyperformance may recreate when it
runs, so patch run_benchmark.py - which lives in the pyperformance
installation and is imported inside that venv.
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_dask/run_benchmark.py",
    "./*/lib/*/site-packages/benchmarks/bm_dask/run_benchmark.py",
    "./*/*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_dask/run_benchmark.py",
    "./*/*/lib/*/site-packages/benchmarks/bm_dask/run_benchmark.py",
]

# Deduplicate by real path in case any entries are symlinks to the same file
seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: bm_dask run_benchmark.py not found - patch failed")
    sys.exit(1)

MARKER = "_pypy_getsizeof"

ANCHOR = "from dask import distributed"

INJECT = ANCHOR + '''

# --- PyPy compatibility patch (buildbot) ---------------------------------
# dask.sizeof's fallback calls sys.getsizeof(obj), which on PyPy raises
# TypeError unless a default is given.  Pass one, so sizing an object dask
# has no registration for does not log a warning for every task result.
import sys as _pypy_sys

_pypy_getsizeof = _pypy_sys.getsizeof


def _pypy_getsizeof_default(obj, default=64):
    return _pypy_getsizeof(obj, default)


_pypy_sys.getsizeof = _pypy_getsizeof_default
# --- end PyPy compatibility patch ----------------------------------------'''

failed = False
for path in files:
    txt = open(path).read()

    if MARKER in txt:
        print("NOTE: %s already patched" % path)
        continue

    if ANCHOR not in txt:
        print("ERROR: %s anchor not found - version mismatch?" % path)
        failed = True
        continue

    txt = txt.replace(ANCHOR, INJECT, 1)
    open(path, "w").write(txt)
    print("Patched %s" % path)

if failed:
    sys.exit(1)
