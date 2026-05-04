"""
Patch SQLAlchemy identity map for PyPy GC compatibility.

PyPy's garbage collector may collect an object referenced by a weakref before
the weakref is checked, causing 'NoneType has no attribute __dict__' errors in
the ORM identity map.  Guard each call to _manage_removed_state with an
explicit liveness check on the weakref.

Source: https://github.com/sqlalchemy/sqlalchemy/discussions/13274
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/sqlalchemy/orm/identity.py",
]

seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: sqlalchemy identity.py not found – patch failed")
    sys.exit(1)

# In replace(): if the GC collected existing, set existing = None so the
# caller gets None back rather than a dead weakref.
OLD_REPLACE = (
    "                if existing is not state:\n"
    "                    self._manage_removed_state(existing)\n"
    "                else:\n"
    "                    return None"
)
NEW_REPLACE = (
    "                if existing is not state:\n"
    "                    if existing.obj() is not None:\n"
    "                        self._manage_removed_state(existing)\n"
    "                    else:\n"
    "                        existing = None\n"
    "                else:\n"
    "                    return None"
)

# In safe_discard(): no return value, so no else clause needed.
OLD_DISCARD = (
    "                    self._manage_removed_state(state)\n"
)
NEW_DISCARD = (
    "                    if state.obj() is not None:\n"
    "                        self._manage_removed_state(state)\n"
)

failed = False
for path in files:
    txt = open(path).read()
    original = txt

    if OLD_REPLACE in txt:
        txt = txt.replace(OLD_REPLACE, NEW_REPLACE)
    elif NEW_REPLACE in txt:
        print(f"NOTE: {path} replace() already patched")
    else:
        print(f"ERROR: {path} replace() pattern not found – version mismatch?")
        failed = True

    if OLD_DISCARD in txt:
        txt = txt.replace(OLD_DISCARD, NEW_DISCARD)
    elif NEW_DISCARD in txt:
        print(f"NOTE: {path} safe_discard() already patched")
    else:
        print(f"ERROR: {path} safe_discard() pattern not found – version mismatch?")
        failed = True

    if txt != original:
        open(path, "w").write(txt)
        print(f"Patched {path}")

if failed:
    sys.exit(1)
