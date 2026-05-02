#!/usr/bin/env python3
"""
Bulk upload benchmark results to a codespeed instance in a single POST.

Supports two input formats:
  legacy  - result.json produced by runner.py (unladen_swallow/perf format)
  pyperf  - result.json produced by pyperformance (pyperf format)

Revision and branch are read from the JSON file when possible (legacy format);
pass --revision / --branch on the command line to override or when using pyperf.

Examples:
  # legacy, jit-off results
  ./bulk_upload.py result.json -e pypy-c-64 -H benchmarker

  # legacy, jit-on results (avg_base values)
  ./bulk_upload.py result.json -e pypy-c-jit-64 -H benchmarker --baseline

  # pyperformance
  ./bulk_upload.py pyperf.json -e pypy-c-64 -H benchmarker -r abc123 -B py3.11
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def detect_format(data):
    if 'benchmarks' in data and isinstance(data.get('benchmarks'), list):
        benchmarks = data['benchmarks']
        if benchmarks and 'runs' in benchmarks[0]:
            return 'pyperf'
    if 'results' in data:
        return 'legacy'
    raise ValueError("Unrecognised result file format")


def parse_legacy(data, changed):
    """Return list of (name, value, std_dev) from a runner.py result.json.

    changed=True  -> upload avg_changed values (jit-off run, executable pypy-c-NN)
    changed=False -> upload avg_base values   (jit-on  run, executable pypy-c-jit-NN)
    """
    records = []
    for name, result_type, result_data in data['results']:
        value = std_dev = None

        if result_type == 'ComparisonResult':
            if changed:
                value = result_data.get('avg_changed')
                std_dev = result_data.get('std_changed')
            else:
                value = result_data.get('avg_base')
                std_dev = result_data.get('std_base')

        elif result_type == 'SimpleComparisonResult':
            value = result_data['changed_time'] if changed else result_data['base_time']

        elif result_type == 'RawResult':
            times = result_data['changed_times'] if changed else result_data['base_times']
            if times:
                value = times[0] if len(times) == 1 else statistics.mean(times)
                std_dev = statistics.stdev(times) if len(times) > 1 else None

        if not value:
            continue
        records.append((name, value, std_dev))
    return records


def parse_pyperf(data):
    """Return (records, suite_version) from a pyperformance/pyperf result.json.

    records is a list of (name, mean_seconds, std_dev_or_None).
    suite_version is taken from the first benchmark's metadata 'version' field.
    """
    records = []
    suite_version = ''

    for bench in data.get('benchmarks', []):
        meta = bench.get('metadata', {})
        name = meta.get('name', '')
        if not name:
            continue
        if not suite_version:
            suite_version = meta.get('version', '')

        # pyperf stores values as floats (seconds) inside each run.
        # Each value entry is either a plain float or a (loops, time_per_loop) pair.
        values = []
        for run in bench.get('runs', []):
            for v in run.get('values', []):
                values.append(v[1] if isinstance(v, (list, tuple)) else v)

        if not values:
            continue

        mean = statistics.mean(values)
        std_dev = statistics.stdev(values) if len(values) > 1 else None
        records.append((name, mean, std_dev))

    return records, suite_version


def build_codespeed_record(name, value, std_dev, args,
                           source='legacy', suite_version=''):
    record = {
        'commitid':     args.revision,
        'branch':       args.branch,
        'project':      args.project,
        'executable':   args.executable,
        'environment':  args.host,
        'benchmark':    name,
        'result_value': value,
        'source':       source,
    }
    if std_dev is not None:
        record['std_dev'] = std_dev
    if suite_version:
        record['suite_version'] = suite_version
    return record


def send_bulk(records, url, username=None, password=None):
    params = urllib.parse.urlencode({'json': json.dumps(records)}).encode('utf-8')

    if username and password:
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, url, username, password)
        opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))
    else:
        opener = urllib.request.build_opener()

    retries = [1, 3, 6, 20]
    while True:
        try:
            req = urllib.request.Request(url + 'result/add/json/', params)
            resp = opener.open(req)
            print(resp.read().decode())
            return
        except urllib.error.URLError:
            if not retries:
                raise
            delay = retries.pop(0)
            print(f"Upload failed, retrying in {delay}s...")
            time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('jsonfile', help='Path to result JSON file')
    parser.add_argument('-e', '--executable', required=True,
                        help='Executable name in codespeed')
    parser.add_argument('-H', '--host', required=True,
                        help='Environment/host name in codespeed')
    parser.add_argument('-r', '--revision', default=None,
                        help='Commit id (read from file if omitted)')
    parser.add_argument('-B', '--branch', default=None,
                        help='Branch name (read from file if omitted)')
    parser.add_argument('-P', '--project', default='PyPy',
                        help='Project name in codespeed (default: PyPy)')
    parser.add_argument('-u', '--url', default='https://speed.pypy.org/',
                        help='Base URL of codespeed instance')
    parser.add_argument('-b', '--baseline', action='store_true',
                        help='Upload avg_base values (legacy format only; '
                             'use for the jit-on executable)')
    args = parser.parse_args()

    with open(args.jsonfile) as f:
        data = json.load(f)

    fmt = detect_format(data)

    if args.revision is None:
        args.revision = data.get('revision') or ''
    if args.branch is None:
        args.branch = data.get('branch') or 'default'

    if not args.revision:
        parser.error('--revision is required (could not read from file)')

    if fmt == 'legacy':
        raw = parse_legacy(data, changed=not args.baseline)
        records = [
            build_codespeed_record(name, value, std_dev, args, source='legacy')
            for name, value, std_dev in raw
        ]
    else:
        raw, suite_version = parse_pyperf(data)
        records = [
            build_codespeed_record(name, value, std_dev, args,
                                   source='pyperformance',
                                   suite_version=suite_version)
            for name, value, std_dev in raw
        ]

    if not records:
        print("No results to upload.", file=sys.stderr)
        sys.exit(1)

    username = os.environ.get('SPEED_UPLOAD_USER')
    password = os.environ.get('SPEED_UPLOAD_PASSWORD')
    auth_note = f" as {username!r}" if username and password else " (no credentials)"

    print(f"Uploading {len(records)} results ({fmt} format) "
          f"for {args.executable!r} to {args.url}{auth_note}")
    send_bulk(records, args.url, username=username, password=password)
    print("Done.")


if __name__ == '__main__':
    main()
