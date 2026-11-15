import json
import os
import time

from pypybuildbot.benchresults import (BenchResultName, BenchResultIndex,
                                       BenchResultLister, extract,
                                       normalize_branch, parse_version)


# -- the filename grammar -------------------------------------------------

def test_name_pypy_legacy():
    n = BenchResultName('189477:e261d90efbb6-64-benchmarker2.json')
    assert n.interp == 'pypy'
    assert n.rev == '189477:e261d90efbb6'
    assert n.numrev == 189477
    assert n.sha == 'e261d90efbb6'
    assert n.bits == '64'
    assert n.host == 'benchmarker2'
    assert n.suite == 'legacy'
    # the legacy file is one run of each
    assert n.jit == 'on+off'
    assert n.tag == ''
    assert n.short_rev == '189477:e261d90efbb6'


def test_name_pypy_pyperformance():
    n = BenchResultName('189477:e261d90efbb6-64-pyperformance.json')
    assert n.suite == 'pyperformance'
    assert n.jit == 'on'
    assert n.bits == '64'
    assert n.host == ''


def test_name_pypy_pyperformance_nojit():
    n = BenchResultName('189477:e261d90efbb6-64-pyperformance-nojit.json')
    assert n.suite == 'pyperformance'
    assert n.jit == 'off'
    assert n.bits == '64'


def test_name_pypy_no_bits_no_host():
    # the oldest files, from before the -64 postfix and the -<host> suffix
    n = BenchResultName('91981:df23f3d1554c.json')
    assert n.rev == '91981:df23f3d1554c'
    assert n.numrev == 91981
    assert n.bits == ''
    assert n.host == ''
    assert n.suite == 'legacy'


def test_name_cpython():
    n = BenchResultName('cpython-3bb231a6a5dc-pyperformance.json')
    assert n.interp == 'cpython'
    assert n.rev == '3bb231a6a5dc'
    assert n.sha == '3bb231a6a5dc'
    assert n.numrev == -1
    assert n.suite == 'pyperformance'
    assert n.host == ''
    assert n.tag == ''
    assert n.short_rev == '3bb231a6a5dc'
    # the jit column is about the pyperformance/-nojit pair pypy runs
    assert n.jit == ''


def test_name_cpython_legacy_host():
    n = BenchResultName('cpython-26badd80b6dc-benchmarker2.json')
    assert n.interp == 'cpython'
    assert n.suite == 'legacy'
    assert n.host == 'benchmarker2'


def test_name_result_tag():
    # result_tag tells two builds of one sha apart, and may contain dashes
    n = BenchResultName('cpython-26badd80b6dc-no-lto-pyperformance.json')
    assert n.sha == '26badd80b6dc'
    assert n.tag == 'no-lto'
    assert n.suite == 'pyperformance'
    n = BenchResultName('cpython-26badd80b6dc-lto-benchmarker2.json')
    assert n.tag == 'lto'
    assert n.host == 'benchmarker2'
    assert n.suite == 'legacy'


def test_name_commit_url():
    assert BenchResultName('189477:e261d90efbb6-64-benchmarker2.json') \
        .commit_url() == 'https://github.com/pypy/pypy/commit/e261d90efbb6'
    assert BenchResultName('cpython-3bb231a6a5dc-pyperformance.json') \
        .commit_url() == 'https://github.com/python/cpython/commit/3bb231a6a5dc'


def test_name_garbage():
    n = BenchResultName('notes.json')
    assert n.rev == 'notes'
    assert n.numrev == -1
    assert n.commit_url() == 'https://github.com/pypy/pypy/commit/notes'


# -- reading the two json formats ----------------------------------------

# Both samples are shaped like the files in ~/bench_results: keys that are
# really there, keys that are really missing.
LEGACY = {
    'revision': '189477:e261d90efbb6',
    'revision_date': '2026-11-15 13:06:52.603542',
    'branch': 'py3.12',
    'options': '',
    'results': [
        ['ai', 'RawResult', {'base_times': [1.0, 1.1]}],
        ['chaos', 'ComparisonResult', {'avg_base': 1.0, 'avg_changed': 2.0}],
    ],
}

PYPERF = {
    'version': '1.0',
    'metadata': {'hostname': 'benchmarker2', 'cpu_count': 9,
                 'cpu_model_name': 'AMD Ryzen 5 3600 6-Core Processor',
                 'performance_version': '1.14.0', 'perf_version': '2.10.0',
                 'unit': 'second'},
    'benchmarks': [
        # 2to3 runs as an external command, so it carries no python_* keys
        {'metadata': {'name': '2to3', 'loops': 1}, 'runs': []},
        {'metadata': {'name': 'chaos',
                      'python_version': 'PyPy 8.0.2-alpha0 (Python 3.12.15)'
                                        ' (64-bit) revision e157f7d03936',
                      'python_implementation': 'pypy'},
         'runs': []},
    ],
}


def write(tmpdir, name, data):
    path = tmpdir.join(name)
    path.write(json.dumps(data))
    return str(path)


def test_parse_version():
    assert parse_version('PyPy 8.0.2-alpha0 (Python 3.12.15) (64-bit)'
                         ' revision e157f7d03936') == (
        '8.0.2-alpha0 (3.12.15)', 'e157f7d03936')
    assert parse_version('3.11.18 (64-bit) revision 26badd80b6d') == (
        '3.11.18', '26badd80b6d')
    assert parse_version('3.12.9') == ('3.12.9', '')
    assert parse_version('') == ('', '')


def test_normalize_branch():
    # hg's nameless branch is what the rest of the site calls main
    assert normalize_branch('', 'pypy') == 'main'
    assert normalize_branch('default', 'pypy') == 'main'
    assert normalize_branch('py3.12', 'pypy') == 'py3.12'
    # 'default' is what the legacy runner writes when it was told nothing,
    # and cpython has no branch of that name
    assert normalize_branch('default', 'cpython') == ''
    assert normalize_branch('main', 'cpython') == 'main'


def test_extract_legacy(tmpdir):
    data = extract(write(tmpdir, 'legacy.json', LEGACY))
    assert data['fmt'] == 'legacy'
    assert data['nbench'] == 2
    assert data['branch'] == 'py3.12'
    assert data['revision'] == '189477:e261d90efbb6'
    assert data['revision_date'] == '2026-11-15 13:06'
    # the legacy format says nothing about the interpreter
    assert data['py_version'] == ''


def test_extract_pyperf(tmpdir):
    data = extract(write(tmpdir, 'perf.json', PYPERF))
    assert data['fmt'] == 'pyperf'
    assert data['nbench'] == 2
    assert data['hostname'] == 'benchmarker2'
    assert data['cpu_count'] == 9
    assert data['cpu_model'] == 'AMD Ryzen 5 3600 6-Core Processor'
    # 2to3 carries no python_* metadata, so these only exist further down
    assert data['py_version'] == '8.0.2-alpha0 (3.12.15)'
    assert data['binary_rev'] == 'e157f7d03936'
    assert data['py_impl'] == 'pypy'
    # the suite version is at the top level, not per benchmark
    assert data['suite_version'] == '1.14.0'
    assert data['branch'] == ''
    assert data['revision_date'] == ''


def test_extract_annotated_pyperf(tmpdir):
    data = dict(PYPERF)
    data['metadata'] = dict(PYPERF['metadata'],
                            buildbot_branch='main',
                            buildbot_build_number=239,
                            buildbot_variant='lto',
                            buildbot_configure_args='--with-lto')
    out = extract(write(tmpdir, 'annotated.json', data))
    assert out['branch'] == 'main'
    assert out['build_number'] == 239
    assert out['variant'] == 'lto'
    assert out['configure_args'] == '--with-lto'


def test_extract_bad_files(tmpdir):
    # a failed upload leaves a 0-byte file, and there are many of those
    empty = tmpdir.join('empty.json')
    empty.write('')
    assert extract(str(empty))['fmt'] == 'bad'

    truncated = tmpdir.join('truncated.json')
    truncated.write('{"results": [')
    assert extract(str(truncated))['fmt'] == 'bad'

    assert extract(str(tmpdir.join('missing.json')))['fmt'] == 'bad'

    alist = tmpdir.join('list.json')
    alist.write('[1, 2, 3]')
    assert extract(str(alist))['fmt'] == 'bad'


# -- the index -----------------------------------------------------------

def test_index_caches_by_stat(tmpdir):
    path = write(tmpdir, 'legacy.json', LEGACY)
    index = BenchResultIndex(str(tmpdir.join('index.json')))

    st = os.stat(path)
    assert index.file_data(str(tmpdir), 'legacy.json', st)['nbench'] == 2
    assert index.dirty

    # a second look at an unchanged file does not read it again
    calls = []
    import pypybuildbot.benchresults as mod
    orig = mod.extract
    mod.extract = lambda p: calls.append(p) or orig(p)
    try:
        index.file_data(str(tmpdir), 'legacy.json', os.stat(path))
        assert calls == []

        # an overwritten file is read again: a re-run of the same revision
        # lands on the same name
        changed = dict(LEGACY, results=[['ai', 'RawResult', {}]])
        tmpdir.join('legacy.json').write(json.dumps(changed))
        os.utime(path, (time.time() + 10, time.time() + 10))
        data = index.file_data(str(tmpdir), 'legacy.json', os.stat(path))
        assert len(calls) == 1
        assert data['nbench'] == 1
    finally:
        mod.extract = orig


def test_index_roundtrip(tmpdir):
    indexfile = str(tmpdir.join('index.json'))
    path = write(tmpdir, 'legacy.json', LEGACY)
    index = BenchResultIndex(indexfile)
    index.file_data(str(tmpdir), 'legacy.json', os.stat(path))
    index.builds['189477:e261d90efbb6'] = {'builder': 'jit-benchmark2',
                                           'number': 7, 'branch': 'py3.12'}
    index.dirty = True
    index.save()
    assert not index.dirty

    again = BenchResultIndex(indexfile)
    assert again.files['legacy.json']['data']['nbench'] == 2
    assert again.builds['189477:e261d90efbb6']['number'] == 7


def test_index_ignores_other_versions(tmpdir):
    indexfile = tmpdir.join('index.json')
    indexfile.write(json.dumps({'version': 'from the future',
                                'files': {'x.json': 'junk'}}))
    assert BenchResultIndex(str(indexfile)).files == {}


def test_index_forgets_deleted_files(tmpdir):
    index = BenchResultIndex(str(tmpdir.join('index.json')))
    index.files = {'gone.json': {}, 'here.json': {}}
    index.forget_missing(['here.json'])
    assert list(index.files) == ['here.json']


def test_build_for_matches_every_revision_spelling(tmpdir):
    index = BenchResultIndex(str(tmpdir.join('index.json')))
    index.builds['189477:e261d90efbb6'] = {'builder': 'b', 'number': 1,
                                           'branch': 'py3.12'}
    index.builds['3bb231a6a5dc'] = {'builder': 'c', 'number': 2, 'branch': 'main'}
    assert index.build_for(BenchResultName(
        '189477:e261d90efbb6-64-benchmarker2.json'))['number'] == 1
    assert index.build_for(BenchResultName(
        'cpython-3bb231a6a5dc-pyperformance.json'))['number'] == 2
    assert index.build_for(BenchResultName('91981:df23f3d1554c.json')) is None


# -- the rows ------------------------------------------------------------

def make_lister(tmpdir):
    index = BenchResultIndex(str(tmpdir.join('bench.index.json')))
    return BenchResultLister(str(tmpdir), index=index)


def test_rows(tmpdir):
    write(tmpdir, '189477:e261d90efbb6-64-benchmarker2.json', LEGACY)
    write(tmpdir, '189477:e261d90efbb6-64-pyperformance.json', PYPERF)
    rows = make_lister(tmpdir).collect_rows()
    assert len(rows) == 2
    byfile = dict((r['filename'], r) for r in rows)

    legacy = byfile['189477:e261d90efbb6-64-benchmarker2.json']
    assert legacy['suite'] == 'legacy'
    assert legacy['branch'] == 'py3.12'
    assert legacy['host'] == 'benchmarker2'
    assert legacy['nbench'] == 2
    assert legacy['interp'] == 'pypy'
    # one legacy file holds the jit-on and the jit-off run both
    assert legacy['jit'] == 'on+off'
    assert legacy['commit_date'] == '2026-11-15 13:06'
    assert not legacy['bad']
    # the version the pyperformance file of the same run recorded
    assert legacy['version'] == '8.0.2-alpha0 (3.12.15)'
    assert legacy['binary_rev'] == 'e157f7d03936'

    perf = byfile['189477:e261d90efbb6-64-pyperformance.json']
    assert perf['suite'] == 'pyperformance'
    assert perf['jit'] == 'on'
    assert perf['version'] == '8.0.2-alpha0 (3.12.15)'
    assert perf['suite_version'] == '1.14.0'
    # pyperf files record no branch or commit date; the legacy file does
    assert perf['branch'] == 'py3.12'
    assert perf['commit_date'] == '2026-11-15 13:06'
    # the url is quoted, the colon in the revision is not url safe
    assert perf['href'] == '189477%3Ae261d90efbb6-64-pyperformance.json'
    assert perf['sortrev'] == 189477


def test_rows_cpython(tmpdir):
    write(tmpdir, 'cpython-3bb231a6a5dc-pyperformance.json', PYPERF)
    row = make_lister(tmpdir).collect_rows()[0]
    assert row['interp'] == 'cpython'
    assert row['jit'] == ''
    assert row['sha'] == '3bb231a6a5dc'
    # nothing to sort a bare sha by, so those rows sort last on this column
    assert row['sortrev'] == ''


def test_rows_newest_first(tmpdir):
    older = write(tmpdir, '91981:df23f3d1554c-64-benchmarker.json', LEGACY)
    newer = write(tmpdir, '189477:e261d90efbb6-64-benchmarker2.json', LEGACY)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    rows = make_lister(tmpdir).collect_rows()
    assert [r['numrev'] for r in rows] == [189477, 91981]


def test_rows_skip_non_json_and_dotfiles(tmpdir):
    write(tmpdir, 'real.json', LEGACY)
    tmpdir.join('README').write('not a result')
    tmpdir.join('.hidden.json').write('{}')
    tmpdir.mkdir('subdir.json')
    rows = make_lister(tmpdir).collect_rows()
    assert [r['filename'] for r in rows] == ['real.json']


def test_rows_mark_bad_files(tmpdir):
    tmpdir.join('189477:e261d90efbb6-64-benchmarker2.json').write('')
    rows = make_lister(tmpdir).collect_rows()
    assert len(rows) == 1
    assert rows[0]['bad']
    assert rows[0]['nbench'] == ''
    assert rows[0]['bytes'] == 0
    # the name still says this much
    assert rows[0]['short_rev'] == '189477:e261d90efbb6'


def test_rows_build_link(tmpdir):
    write(tmpdir, '189477:e261d90efbb6-64-benchmarker2.json', LEGACY)
    lister = make_lister(tmpdir)
    lister.index.builds['189477:e261d90efbb6'] = {
        'builder': 'jit-benchmark2-linux-x86-64', 'number': 239,
        'branch': 'py3.12'}
    row = lister.collect_rows()[0]
    assert row['build_number'] == 239
    assert row['build_url'] == 'jit-benchmark2-linux-x86-64/builds/239'
