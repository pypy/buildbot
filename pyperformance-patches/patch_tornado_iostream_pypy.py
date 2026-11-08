"""
Patch tornado's BaseIOStream for PyPy compatibility (needed by bm_dask and
bm_tornado_http).

tornado reads into a bytearray and then resizes it while an unnamed
memoryview of it is still around::

    b = (memoryview(self._read_buffer)[:loc]).tobytes()
    ...
    del self._read_buffer[:loc]

On CPython the temporary view is freed by refcounting before the resize.  On
PyPy it lives until the next collection, so the resize raises::

    BufferError: Existing exports of data: object cannot be re-sized

which kills the benchmark ("Benchmark died"): in bm_dask the worker never
finishes its handshake with the scheduler ("RuntimeError: Worker failed to
start"), and the same happens to bm_tornado_http.

There are three such places in tornado 6.4.x and 6.5.x: read_into(),
_read_to_buffer() and _consume().  Which one bites depends on the distributed
version (older ones read through read_bytes/_consume, newer ones through
read_into), so patch all three, the way tornado 6.6 did upstream: name the
views and release them with a `with` block before the resize.  A tornado that
already has those fixes (6.6 and later) is left alone.
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/tornado/iostream.py",
    "./*/*/lib/*/site-packages/tornado/iostream.py",
]

# Deduplicate by real path in case any entries are symlinks to the same file
seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: tornado/iostream.py not found - patch failed")
    sys.exit(1)

MARKER = "# PyPy: release the view(s) before the bytearray is resized"

# read_into(): copy out of the read buffer, then shrink it
OLD_READ_INTO = """        if available_bytes >= n:
            buf[:] = memoryview(self._read_buffer)[:n]
            del self._read_buffer[:n]
            self._after_user_read_buffer = self._read_buffer
        elif available_bytes > 0:
            buf[:available_bytes] = memoryview(self._read_buffer)[:]
"""
NEW_READ_INTO = """        if available_bytes >= n:
            # PyPy: release the view(s) before the bytearray is resized
            with memoryview(self._read_buffer) as _m, _m[:n] as _src:
                buf[:] = _src
            del self._read_buffer[:n]
            self._after_user_read_buffer = self._read_buffer
        elif available_bytes > 0:
            with memoryview(self._read_buffer) as _m:
                buf[:available_bytes] = _m
"""

# _read_to_buffer(): read straight into the tail of the caller's buffer.  The
# view has to go away before the caller resizes that buffer, and `buf` is
# pre-bound to None because read_from_fd() may now raise before it is set.
OLD_READ_TO_BUFFER = """        try:
            while True:
                try:
                    if self._user_read_buffer:
                        buf = memoryview(self._read_buffer)[
                            self._read_buffer_size :
                        ]  # type: Union[memoryview, bytearray]
                    else:
                        buf = bytearray(self.read_chunk_size)
                    bytes_read = self.read_from_fd(buf)
"""
NEW_READ_TO_BUFFER = """        buf = None  # type: Union[memoryview, bytearray, None]
        try:
            while True:
                try:
                    if self._user_read_buffer:
                        # PyPy: release the view(s) before the bytearray is
                        # resized
                        with memoryview(self._read_buffer) as _m, _m[
                            self._read_buffer_size :
                        ] as _view:
                            bytes_read = self.read_from_fd(_view)
                    else:
                        buf = bytearray(self.read_chunk_size)
                        bytes_read = self.read_from_fd(buf)
"""

# _consume(): hand out the first loc bytes, then shrink the buffer
OLD_CONSUME = """        b = (memoryview(self._read_buffer)[:loc]).tobytes()
        self._read_buffer_size -= loc
        del self._read_buffer[:loc]
"""
NEW_CONSUME = """        # PyPy: release the view(s) before the bytearray is resized
        with memoryview(self._read_buffer) as _m, _m[:loc] as _view:
            b = _view.tobytes()
        self._read_buffer_size -= loc
        del self._read_buffer[:loc]
"""

REPLACEMENTS = [
    ("read_into", OLD_READ_INTO, NEW_READ_INTO),
    ("_read_to_buffer", OLD_READ_TO_BUFFER, NEW_READ_TO_BUFFER),
    ("_consume", OLD_CONSUME, NEW_CONSUME),
]

# what tornado >= 6.6 (which fixed this itself) looks like
ALREADY_FIXED = "with memoryview(self._read_buffer) as m, m[:n] as src:"

failed = False
for path in files:
    txt = open(path).read()

    if MARKER in txt:
        print("NOTE: %s already patched" % path)
        continue

    applied = []
    missing = []
    for name, old, new in REPLACEMENTS:
        if txt.count(old) == 1:
            txt = txt.replace(old, new)
            applied.append(name)
        else:
            missing.append(name)

    if not applied:
        if ALREADY_FIXED in txt:
            print("NOTE: %s releases its views already (tornado >= 6.6)" % path)
        else:
            print("ERROR: %s no known pattern found - version mismatch?" % path)
            failed = True
        continue

    if missing:
        print("ERROR: %s patched %s but did not find %s - version mismatch?"
              % (path, ", ".join(applied), ", ".join(missing)))
        failed = True
        continue

    compile(txt, path, "exec")      # refuse to write a broken iostream.py
    open(path, "w").write(txt)
    print("Patched %s (%s)" % (path, ", ".join(applied)))

if failed:
    sys.exit(1)
