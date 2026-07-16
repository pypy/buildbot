"""
Patch pyperformance bm_tornado_http for PyPy compatibility.

tornado's BaseIOStream._consume does::

    b = (memoryview(self._read_buffer)[:loc]).tobytes()
    ...
    del self._read_buffer[:loc]

On CPython the temporary memoryview is freed by refcounting before the
bytearray is resized.  On PyPy it may still be alive, so resizing the
bytearray raises::

    BufferError: Existing exports of data: object cannot be re-sized

which kills the tornado_http benchmark ("Benchmark died").  Inject a
monkeypatch into the benchmark that reinstalls a _consume which releases the
memoryview before the resize.  tornado is only present in the per-benchmark
venv (created at run time), so we cannot patch tornado directly here; instead
we patch run_benchmark.py, which is imported inside that venv.
"""
import glob
import os
import sys

GLOBS = [
    "./*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_tornado_http/run_benchmark.py",
    "./*/lib/*/site-packages/benchmarks/bm_tornado_http/run_benchmark.py",
    "./*/*/lib/*/site-packages/pyperformance/data-files/benchmarks/bm_tornado_http/run_benchmark.py",
    "./*/*/lib/*/site-packages/benchmarks/bm_tornado_http/run_benchmark.py",
]

# Deduplicate by real path in case any entries are symlinks to the same file
seen = {}
for pattern in GLOBS:
    for path in glob.glob(pattern):
        seen[os.path.realpath(path)] = path
files = list(seen.values())

if not files:
    print("ERROR: bm_tornado_http run_benchmark.py not found - patch failed")
    sys.exit(1)

MARKER = "_pypy_consume"

ANCHOR = "from tornado.web import RequestHandler, Application"

INJECT = ANCHOR + '''

# --- PyPy compatibility patch (buildbot) ---------------------------------
# tornado's _consume slices self._read_buffer via an unnamed base memoryview
# that lingers on PyPy, blocking the `del` resize (BufferError). Name and
# release both views before resizing.
import tornado.iostream as _pypy_iostream


def _pypy_consume(self, loc):
    if loc == 0:
        return b""
    assert loc <= self._read_buffer_size
    _mv = memoryview(self._read_buffer)
    _view = _mv[:loc]
    _b = _view.tobytes()
    _view.release()
    _mv.release()
    self._read_buffer_size -= loc
    del self._read_buffer[:loc]
    return _b


_pypy_iostream.BaseIOStream._consume = _pypy_consume
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
