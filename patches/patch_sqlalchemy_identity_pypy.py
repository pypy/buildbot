"""
Patch SQLAlchemy identity map for PyPy GC compatibility.

PyPy's garbage collector may collect an object referenced by a weakref before
the weakref is checked, causing 'NoneType has no attribute __dict__' errors in
the ORM identity map.  Guard the call to _manage_removed_state with an
explicit liveness check on the weakref.

Source: https://github.com/sqlalchemy/sqlalchemy/discussions/13274
"""
import glob
import re
import sys

GLOB = ("./venv/*/lib/python*/site-packages"
        "/sqlalchemy/orm/identity.py")

files = glob.glob(GLOB)
if not files:
    print("ERROR: sqlalchemy identity.py not found – patch failed")
    sys.exit(1)

# Replace every bare call to _manage_removed_state(existing_non_none) with a
# weakref liveness check.  Capture indentation so the replacement is correctly
# indented regardless of nesting level.
PATTERN = re.compile(
    r"^( +)self\._manage_removed_state\(existing_non_none\)\s*$",
    re.MULTILINE,
)


def _replacement(m):
    i = m.group(1)
    return (
        f"{i}if existing_non_none.obj() is not None:\n"
        f"{i}    self._manage_removed_state(existing_non_none)\n"
        f"{i}else:\n"
        f"{i}    existing = None"
    )


for path in files:
    txt = open(path).read()
    new_txt, count = PATTERN.subn(_replacement, txt)

    if count == 0:
        print(f"NOTE: {path} already patched or pattern not found")
    else:
        open(path, "w").write(new_txt)
        print(f"Patched {path} ({count} site(s))")
