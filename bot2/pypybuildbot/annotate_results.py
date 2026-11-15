"""Normalise the metadata in a benchmark result file, in place.

Two things, both aimed at whoever reads these files later:

Hoisting.  pyperf records which interpreter it measured in *each* benchmark's
metadata and only lifts a key to the top level when every benchmark in the
suite carries it.  2to3, python_startup and python_startup_no_site are run as
external commands, so they have no python_* metadata at all, and those three
alone are enough to keep the whole file's top-level metadata down to host
facts.  Here a key is hoisted when the benchmarks that do define it agree.

Stamping.  --set adds what only the build knows: the configure line it was
forced with, the sha, the variant tag.  The legacy file gets its metadata from
run_local.py, which cannot see any of that.

Nothing is removed and no existing top-level key is overwritten.
"""

import json
import os
import sys


# What hoisting considers: facts about the interpreter under test, never the
# per-benchmark bookkeeping (name, loops, command, ...) around them.
HOIST_PREFIXES = ('python_', 'pypy_')


def hoist(data, md):
    benchmarks = data.get('benchmarks')
    if not isinstance(benchmarks, list):
        return {}
    values = {}
    for bench in benchmarks:
        if not isinstance(bench, dict):
            continue
        for key, value in (bench.get('metadata') or {}).items():
            if key in md or not key.startswith(HOIST_PREFIXES):
                continue
            values.setdefault(key, []).append(value)
    hoisted = {}
    for key, found in values.items():
        first = found[0]
        # json scalars and containers both compare by value
        if all(other == first for other in found[1:]):
            hoisted[key] = first
    return hoisted


def annotate(path, stamps):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError('top level is %s, not an object' % type(data).__name__)

    md = data.get('metadata')
    if md is None:
        md = data['metadata'] = {}
    elif not isinstance(md, dict):
        raise ValueError("'metadata' is %s, not an object" % type(md).__name__)

    added = hoist(data, md)
    added.update((k, v) for k, v in stamps.items() if k not in md)
    md.update(added)

    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)
    return added


def main(argv):
    stamps = {}
    paths = []
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == '--set':
            if not rest:
                sys.exit('--set needs KEY=VALUE')
            key, sep, value = rest.pop(0).partition('=')
            if not sep:
                sys.exit('--set needs KEY=VALUE')
            stamps[key] = value
        else:
            paths.append(arg)
    if not paths:
        sys.exit(__doc__.strip().splitlines()[0])

    failed = False
    for path in paths:
        try:
            added = annotate(path, stamps)
        except Exception as e:
            print('%s: not annotated: %s' % (path, e))
            failed = True
            continue
        print('%s: added %d metadata keys' % (path, len(added)))
        for key in sorted(added):
            print('    %-26s %s' % (key, added[key]))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
