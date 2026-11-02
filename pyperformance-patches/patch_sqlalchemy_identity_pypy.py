"""
Patch bm_sqlalchemy_declarative for PyPy GC compatibility.

SQLite reuses primary key IDs after a full table DELETE; the next loop
iteration inserts new objects with the same IDs as the previous run.  On
CPython, refcounting immediately collects the old objects, clearing the
identity map's weakrefs.  On PyPy, GC is deferred, so the old objects
remain alive and SQLAlchemy emits a SAWarning for every inserted row on
every loop, flooding stderr and causing the benchmark to fail.

Fix: call session.expunge_all() after the bulk deletes to explicitly clear
stale session state before timing begins.

Source: https://github.com/python/pyperformance/pull/472
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_sqlalchemy_declarative/run_benchmark.py",
    "./*/lib/*/site-packages/benchmarks/bm_sqlalchemy_declarative/run_benchmark.py",
    "./*/*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_sqlalchemy_declarative/run_benchmark.py",
    "./*/*/lib/*/site-packages/benchmarks/bm_sqlalchemy_declarative/run_benchmark.py",
]

seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: bm_sqlalchemy_declarative/run_benchmark.py not found – patch failed")
    sys.exit(1)

OLD = (
    "        session.query(Person).delete(synchronize_session=False)\n"
    "        session.query(Address).delete(synchronize_session=False)\n"
)
NEW = (
    "        session.query(Person).delete(synchronize_session=False)\n"
    "        session.query(Address).delete(synchronize_session=False)\n"
    "        session.expunge_all()\n"
)

failed = False
for path in files:
    txt = open(path).read()
    original = txt

    if OLD in txt:
        txt = txt.replace(OLD, NEW)
    elif NEW in txt:
        print(f"NOTE: {path} already patched")
    else:
        print(f"ERROR: {path} pattern not found – version mismatch?")
        failed = True

    if txt != original:
        open(path, "w").write(txt)
        print(f"Patched {path}")

if failed:
    sys.exit(1)
