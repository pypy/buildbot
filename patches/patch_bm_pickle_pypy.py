"""
Patch pyperformance bm_pickle for PyPy compatibility.

PyPy provides a _pickle accelerator module, so the existing IS_PYPY check
in the pure-python guard incorrectly raises RuntimeError.  Move IS_PYPY to
the accelerated-module check instead so PyPy can use its C extension.

Source: https://github.com/python/pyperformance/pull/461
"""
import glob
import sys

GLOB = ("./venv/*/lib/python*/site-packages"
        "/pyperformance/data-files/benchmarks/bm_pickle/run_benchmark.py")

files = glob.glob(GLOB)
if not files:
    print("ERROR: bm_pickle run_benchmark.py not found – patch failed")
    sys.exit(1)

for path in files:
    txt = open(path).read()
    original = txt

    txt = txt.replace(
        "if not (options.pure_python or IS_PYPY):",
        "if not (options.pure_python):",
    )
    txt = txt.replace(
        "if not is_accelerated_module(pickle):",
        "if not is_accelerated_module(pickle) and not IS_PYPY:",
    )

    if txt == original:
        print(f"NOTE: {path} already patched or pattern not found")
    else:
        open(path, "w").write(txt)
        print(f"Patched {path}")
