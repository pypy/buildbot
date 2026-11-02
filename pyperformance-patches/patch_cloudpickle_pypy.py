"""
Patch cloudpickle for PyPy compatibility (needed by bm_dask).

cloudpickle picks between two picklers: the modern reducer_override path for
the C pickler, and a legacy path that copies pickle.Pickler.dispatch.  It
chooses with platform.python_implementation() == "PyPy", assuming PyPy always
uses the pure-python pickle._Pickler.  PyPy 3.12 ships a _pickle accelerator,
so pickle.Pickler has no 'dispatch' and importing cloudpickle dies with
AttributeError.  Test for the attribute the legacy path needs instead; PyPy
then takes the reducer_override path, which it supports.

Source: https://github.com/cloudpipe/cloudpickle/issues/592
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/cloudpickle/cloudpickle.py",
    "./*/*/lib/*/site-packages/cloudpickle/cloudpickle.py",
]

seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: cloudpickle/cloudpickle.py not found - patch failed")
    sys.exit(1)

OLD = (
    "    if not PYPY:\n"
    "        # pickle.Pickler is the C implementation of the CPython pickler and\n"
)
NEW = (
    '    if not hasattr(pickle.Pickler, "dispatch"):\n'
    "        # pickle.Pickler is the C implementation of the CPython pickler and\n"
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
        print(f"ERROR: {path} pattern not found - version mismatch?")
        failed = True

    if txt != original:
        open(path, "w").write(txt)
        print(f"Patched {path}")

if failed:
    sys.exit(1)
