"""Custom rendering of ~/bench_results, the raw benchmark result archive.

The stock twisted.web.static listing shows a name, a size and a content type,
and the name is url-quoted, so the one column that carries information is
unreadable.  Everything a reader actually wants -- which interpreter, which
branch, which suite, how many benchmarks survived the run -- is either encoded
in the filename or inside the json, so this module parses both and renders a
sortable table (see master/templates/bench_results.html).

Two file layouts live in that directory:

    <count>:<sha>[-<bits>][-<tag>]-<host>.json           pypy, legacy suite
    <count>:<sha>[-<bits>]-pyperformance[-nojit].json    pypy, pyperformance
    cpython-<sha>[-<tag>]-<host>.json                    cpython, legacy suite
    cpython-<sha>[-<tag>]-pyperformance.json             cpython, pyperformance

and two json formats behind them: the legacy unladen-swallow shape that
runner.py writes (a 'results' list, with 'revision' and 'branch' at the top
level) and the pyperf shape that pyperformance writes (a 'benchmarks' list,
with run metadata per benchmark and the host facts at the top level).  Reading
them is the same job bulk_upload.py does, kept deliberately separate: that one
uploads, this one only describes.

Cost.  There are a few thousand files of ~25kB each, so a full parse is seconds
of work and must not happen in the reactor thread on every hit: the extracted
fields are cached in ~/bench_results.index.json keyed by (mtime, size), the
scan itself runs in a thread, and only files whose stat changed are re-read.
The same index caches revision -> build number for the benchmark builders,
which otherwise costs one pickle load per build.
"""

import cgi
import datetime
import json
import os
import re
import urllib

from twisted.internet import threads
from twisted.python import log
from twisted.web import server
from twisted.web.static import File, formatFileSize

from buildbot.status.web.base import DirectoryLister, ContextMixin


# Bump when the shape of a cached entry changes: a stale index is then dropped
# wholesale instead of feeding half-filled rows to the template.
INDEX_VERSION = 2

SUITE_PYPERFORMANCE = 'pyperformance'
SUITE_LEGACY = 'legacy'

# The forge each sha belongs to, for the commit links.
REPO_URLS = {
    'pypy': 'https://github.com/pypy/pypy',
    'cpython': 'https://github.com/python/cpython',
}

# Only these builders can have produced a file here, and scanning every
# builder's history would be pointless work.  Matched as a substring so
# renames like jit-benchmark2-linux-x86-64 keep working.
BUILDER_MARKER = 'benchmark'

# got_revision under its various names.  The cpython factory keeps its revision
# in its own property because the build has two checkouts (cpython and the
# benchmark repo) and 'got_revision' is the wrong one.
REVISION_PROPERTIES = ('got_revision', 'cpython_got_revision',
                       'cpython_got_revision_short')
BRANCH_PROPERTIES = ('branch', 'cpython_branch')


class BenchResultName(object):
    """What the filename alone says about a result file.

    Parsed right to left, because only the tail is a fixed vocabulary: the
    suite and jit markers are literals, the host is whatever the factory was
    configured with, and anything left over in the middle is the result_tag of
    a build variant (which may itself contain dashes).
    """

    def __init__(self, filename):
        self.filename = filename
        self.interp = 'pypy'
        self.rev = ''
        self.sha = ''
        self.numrev = -1
        self.bits = ''
        self.suite = SUITE_LEGACY
        self.jit = ''
        self.host = ''
        self.tag = ''
        self._parse()

    def _parse(self):
        name = self.filename
        if name.endswith('.json'):
            name = name[:-len('.json')]
        if name.startswith('cpython-'):
            self.interp = 'cpython'
            name = name[len('cpython-'):]

        parts = name.split('-')
        head = parts.pop(0)

        if parts and parts[-1] == 'nojit':
            parts.pop()
            self.jit = 'off'
        if parts and parts[-1] == SUITE_PYPERFORMANCE:
            parts.pop()
            self.suite = SUITE_PYPERFORMANCE
            # A pyperformance file is the jit-on run of the pair whose other
            # half is -nojit; cpython has no such axis, so it gets no value.
            if self.interp == 'pypy':
                self.jit = self.jit or 'on'
        elif parts and not parts[-1].isdigit():
            self.host = parts.pop()
        if parts and parts[-1].isdigit():
            self.bits = parts.pop()
        self.tag = '-'.join(parts)

        if self.suite == SUITE_LEGACY and self.interp == 'pypy':
            # One legacy file holds both halves of a ComparisonResult: the
            # jit-on run in avg_base and the jit-off run in avg_changed, which
            # is why it is uploaded twice under two codespeed executables.
            self.jit = 'on+off'

        # 'count:sha' for pypy (the sortable half is the commit count the
        # CheckGotRevision step prepends), a bare sha for cpython.
        self.rev = head
        if ':' in head:
            count, self.sha = head.rsplit(':', 1)
            try:
                self.numrev = int(count)
            except ValueError:
                self.numrev = -1
        else:
            self.sha = head

    @property
    def short_rev(self):
        """The revision, with the sha cut to the usual 12 characters."""
        if self.numrev >= 0:
            return '%d:%s' % (self.numrev, self.sha[:12])
        return self.sha[:12]

    def commit_url(self):
        repo = REPO_URLS.get(self.interp)
        if not repo or not self.sha:
            return ''
        return '%s/commit/%s' % (repo, self.sha)


def _first_defined(benchmarks, key):
    """The value of a per-benchmark metadata key, from the first run that has it.

    pyperf only lifts a key to the top level when every benchmark carries it,
    and 2to3/python_startup are run as external commands and carry no
    python_* metadata at all, so python_version is usually only visible down
    here.  annotate_results.py hoists these for new files; old files keep
    needing this.
    """
    for bench in benchmarks:
        if not isinstance(bench, dict):
            continue
        value = (bench.get('metadata') or {}).get(key)
        if value not in (None, ''):
            return value
    return ''


_BINARY_REV_RE = re.compile(r'\brevision\s+([0-9a-fA-F]+)\s*$')
_BITS_RE = re.compile(r'\s*\(\d+-bit\)')
_PYPY_BANNER_RE = re.compile(r'^PyPy\s+(\S+)\s+\(Python\s+([^)]+)\)$')


def parse_version(banner):
    """Split a pyperf python_version into (what to show, the binary's revision).

    pyperf records the interpreter's own banner, which for the benchmarked
    builds looks like

        PyPy 8.0.2-alpha0 (Python 3.12.15) (64-bit) revision e157f7d03936
        3.11.18 (64-bit) revision 26badd80b6d

    The revision in there is the commit the *binary* was built from.  It is
    worth keeping separate because it is not always the revision the file is
    filed under: a benchmark build checks out one commit and benchmarks the
    nightly it downloaded, which may be an older one.
    """
    if not banner:
        return '', ''
    rev = ''
    match = _BINARY_REV_RE.search(banner)
    if match:
        rev = match.group(1)
        banner = banner[:match.start()]
    banner = _BITS_RE.sub('', banner).strip().rstrip(',').strip()
    match = _PYPY_BANNER_RE.match(banner)
    if match:
        banner = '%s (%s)' % (match.group(1), match.group(2))
    return banner, rev


def normalize_branch(branch, interp):
    """The branch as the rest of the site spells it.

    summary.py maps hg's nameless default branch to 'main' and so does
    build-summary, so a pypy file that says 'default' or nothing is on main.
    A cpython file saying 'default' says nothing at all: that is the fallback
    the legacy runner writes when the build told it no branch.
    """
    branch = branch or ''
    if interp == 'pypy':
        return 'main' if branch in ('', 'default') else branch
    return '' if branch == 'default' else branch


def extract(path):
    """The display fields of one result file.  Missing values come back as ''.

    Raises nothing the caller has to distinguish: a file that cannot be read or
    parsed (a 0-byte failed upload, of which there are many) gets fmt='bad',
    which the page shows as such instead of as an empty row.
    """
    out = {'fmt': 'bad', 'branch': '', 'revision': '', 'revision_date': '',
           'nbench': '', 'py_version': '', 'py_version_full': '',
           'py_impl': '', 'binary_rev': '', 'suite_version': '',
           'hostname': '', 'cpu_count': '', 'cpu_model': '', 'unit': '',
           'build_number': '', 'builder': '', 'variant': '',
           'configure_args': ''}
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return out
    if not isinstance(data, dict):
        return out

    md = data.get('metadata')
    if not isinstance(md, dict):
        md = {}

    benchmarks = data.get('benchmarks')
    results = data.get('results')

    if isinstance(benchmarks, list):
        out['fmt'] = 'pyperf'
        out['nbench'] = len(benchmarks)
        banner = (md.get('python_version')
                  or _first_defined(benchmarks, 'python_version'))
        out['py_version_full'] = banner
        out['py_version'], out['binary_rev'] = parse_version(banner)
        out['py_impl'] = (md.get('python_implementation')
                          or _first_defined(benchmarks, 'python_implementation'))
        # pyperf keeps the suite's own version at the top level; the per
        # benchmark 'version' key that bulk_upload.py looks for is not in
        # the files these builders produce.
        out['suite_version'] = (md.get('performance_version')
                                or _first_defined(benchmarks, 'version'))
    elif isinstance(results, list):
        out['fmt'] = 'legacy'
        out['nbench'] = len(results)
        # The legacy runner records what it benchmarked; pyperf does not.
        # It knows nothing about the interpreter's version, though.
        out['revision'] = data.get('revision') or ''
        out['branch'] = data.get('branch') or ''
        out['revision_date'] = (data.get('revision_date') or '')[:16]
    else:
        return out

    out['hostname'] = md.get('hostname') or ''
    out['cpu_count'] = md.get('cpu_count') or ''
    out['cpu_model'] = md.get('cpu_model_name') or ''
    out['unit'] = md.get('unit') or ''
    # Written by annotate_results.py --set, so only on files produced after
    # that step landed.  Where present it beats every guess made from the name.
    out['branch'] = md.get('buildbot_branch') or out['branch']
    out['revision'] = md.get('buildbot_revision') or out['revision']
    out['build_number'] = md.get('buildbot_build_number') or ''
    out['builder'] = md.get('buildbot_builder') or ''
    out['variant'] = md.get('buildbot_variant') or ''
    out['configure_args'] = md.get('buildbot_configure_args') or ''
    return out


class BenchResultIndex(object):
    """The persistent side of the page: parsed json fields, and build numbers.

    Both are caches of immutable facts -- a result file is written once and a
    finished build never changes its revision -- so entries are only ever
    added, and the json half is invalidated by (mtime, size) for the rare
    case of a file being overwritten by a re-run.
    """

    def __init__(self, path):
        self.path = path
        self.files = {}            # filename -> {'mtime', 'size', 'data'}
        self.builds = {}           # revision -> {'builder', 'number', 'branch'}
        self.scanned = {}          # builder name -> highest build scanned
        self.dirty = False
        self.load()

    def load(self):
        try:
            with open(self.path) as f:
                blob = json.load(f)
        except Exception:
            return
        if not isinstance(blob, dict) or blob.get('version') != INDEX_VERSION:
            return
        self.files = blob.get('files') or {}
        self.builds = blob.get('builds') or {}
        self.scanned = blob.get('scanned') or {}

    def save(self):
        if not self.dirty:
            return
        blob = {'version': INDEX_VERSION, 'files': self.files,
                'builds': self.builds, 'scanned': self.scanned}
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(blob, f)
            os.rename(tmp, self.path)
        except Exception:
            log.err(None, 'bench_results: could not write %s' % (self.path,))
            return
        self.dirty = False

    # -- the json half, called in a worker thread ------------------------

    def file_data(self, dirname, filename, st):
        entry = self.files.get(filename)
        if (entry and entry.get('mtime') == int(st.st_mtime)
                and entry.get('size') == st.st_size):
            return entry['data']
        data = extract(os.path.join(dirname, filename))
        self.files[filename] = {'mtime': int(st.st_mtime),
                                'size': st.st_size, 'data': data}
        self.dirty = True
        return data

    def forget_missing(self, filenames):
        known = set(filenames)
        for gone in [name for name in self.files if name not in known]:
            del self.files[gone]
            self.dirty = True

    # -- the build half, called in the reactor thread --------------------

    def update_builds(self, status):
        """Index the builds of the benchmark builders that are new since last time.

        One pickle load per build, so the first call after a fresh index costs
        a second or two and later ones cost nothing.  Unfinished builds are
        skipped rather than cached: their got_revision is not final.
        """
        if status is None:
            return
        for name in status.getBuilderNames():
            if BUILDER_MARKER not in name:
                continue
            try:
                builder = status.getBuilder(name)
            except Exception:
                continue
            last = int(self.scanned.get(name, -1))
            latest = getattr(builder, 'nextBuildNumber', 0) - 1
            for number in range(last + 1, latest + 1):
                try:
                    build = builder.getBuild(number)
                except Exception:
                    build = None
                if build is None:
                    # Pruned or never written; nothing will appear later.
                    self.scanned[name] = number
                    self.dirty = True
                    continue
                if not build.isFinished():
                    break
                self._record(name, build)
                self.scanned[name] = number
                self.dirty = True

    def _record(self, buildername, build):
        props = build.getProperties()
        branch = ''
        for key in BRANCH_PROPERTIES:
            branch = props.getProperty(key, '') or branch
            if branch:
                break
        entry = {'builder': buildername, 'number': build.getNumber(),
                 'branch': branch}
        for key in REVISION_PROPERTIES:
            rev = props.getProperty(key, None)
            if rev:
                self.builds[str(rev)] = entry

    def build_for(self, name):
        """The build that produced a file, looked up by every revision spelling."""
        for key in (name.rev, name.sha, name.sha[:12]):
            if key and key in self.builds:
                return self.builds[key]
        return None


class BenchResultList(File):
    """~/bench_results, listed by BenchResultLister; children served as files."""

    def __init__(self, path, *args, **kwargs):
        File.__init__(self, path, *args, **kwargs)
        # Beside the directory, not in it: everything under self.path is served.
        self.index = BenchResultIndex(path.rstrip(os.path.sep) + '.index.json')

    def directoryListing(self):
        return BenchResultLister(self.path,
                                 self.listNames(),
                                 self.contentTypes,
                                 self.contentEncodings,
                                 self.defaultType,
                                 index=self.index)


class BenchResultLister(DirectoryLister, ContextMixin):
    """Renders master/templates/bench_results.html.

    render() returns NOT_DONE_YET and does the scan in a thread: with a few
    thousand files even a fully cached render is a few thousand stat calls,
    and a cold one parses ~50MB of json.  Neither belongs in the reactor on a
    page anyone can hit.
    """

    pageTitle = 'Benchmark results'

    def __init__(self, path, dirs=None,
                 contentTypes=File.contentTypes,
                 contentEncodings=File.contentEncodings,
                 defaultType='text/html', index=None):
        DirectoryLister.__init__(self, path, dirs, contentTypes,
                                 contentEncodings, defaultType)
        self.index = index if index is not None else BenchResultIndex(
            path.rstrip(os.path.sep) + '.index.json')

    def render(self, request):
        cxt = self.getContext(request)
        self.index.update_builds(self.getStatus(request))
        templates = request.site.buildbot_service.templates

        d = threads.deferToThread(self.collect_rows)

        def write(rows):
            self.index.save()
            cxt['path'] = cgi.escape(urllib.unquote(request.uri))
            cxt['rows'] = rows
            template = templates.get_template('bench_results.html')
            data = template.render(**cxt)
            if isinstance(data, unicode):
                data = data.encode('utf-8')
            request.setHeader('content-type', 'text/html; charset=utf-8')
            request.write(data)

        def finish(_):
            if not getattr(request, '_disconnected', False):
                request.finish()

        d.addCallback(write)
        d.addErrback(lambda f: log.err(f, 'bench_results listing failed'))
        d.addBoth(finish)
        return server.NOT_DONE_YET

    def collect_rows(self):
        """One row per result file, newest first.  Runs in a worker thread."""
        names = self.dirs if self.dirs is not None else os.listdir(self.path)
        filenames = [n for n in names
                     if n.endswith('.json') and not n.startswith('.')]
        self.index.forget_missing(filenames)

        rows = []
        for filename in filenames:
            full = os.path.join(self.path, filename)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            rows.append(self.make_row(filename, st,
                                      self.index.file_data(self.path,
                                                           filename, st)))

        self.fill_from_siblings(rows)
        rows.sort(key=lambda r: (r['mtime'], r['numrev']), reverse=True)
        return rows

    @staticmethod
    def fill_from_siblings(rows):
        """Let the files of one run fill in each other's blanks.

        One build uploads a legacy file and the two pyperformance files, and
        each format records half of what the page wants: the legacy runner
        writes the branch and the commit date and knows nothing about the
        interpreter, pyperf records the interpreter's banner and nothing about
        the checkout.  Keyed on revision and bitness, so the two machines that
        once benchmarked the same revision stay apart -- except that only the
        legacy suite ever ran on both, so a borrowed banner always comes from
        the same build as the file it fills.
        """
        branches, versions, dates = {}, {}, {}
        for row in rows:
            key = (row['rev'], row['bits'])
            if row['branch']:
                branches.setdefault(key, row['branch'])
            if row['version']:
                versions.setdefault(key, (row['version'], row['version_full'],
                                          row['binary_rev']))
            if row['commit_date']:
                dates.setdefault(key, row['commit_date'])
        for row in rows:
            key = (row['rev'], row['bits'])
            if not row['branch']:
                row['branch'] = branches.get(key, '')
            if not row['version'] and key in versions:
                (row['version'], row['version_full'],
                 row['binary_rev']) = versions[key]
            if not row['commit_date']:
                row['commit_date'] = dates.get(key, '')
            # last, so that a borrowed branch is normalized too and an empty
            # one does not first turn into pypy's 'main' and block the borrow
            row['branch'] = normalize_branch(row['branch'], row['interp'])

    def make_row(self, filename, st, data):
        name = BenchResultName(filename)
        build = self.index.build_for(name)
        date = datetime.datetime.fromtimestamp(st.st_mtime)
        branch = data['branch'] or (build or {}).get('branch', '')

        return {
            'filename': filename,
            'href': urllib.quote(filename),
            'rev': name.rev,
            'short_rev': name.short_rev,
            'sha': name.sha,
            'numrev': name.numrev,
            # cpython revisions have no sortable half, and an empty sort key
            # puts those rows after the numbered ones either way round
            'sortrev': name.numrev if name.numrev >= 0 else '',
            'commit_url': name.commit_url(),
            'interp': name.interp,
            'bits': name.bits,
            # normalized in fill_from_siblings, once borrowing is done
            'branch': branch,
            'version': data['py_version'],
            'version_full': data['py_version_full'],
            'binary_rev': data['binary_rev'],
            'commit_date': data['revision_date'],
            'suite': name.suite,
            'jit': name.jit,
            'variant': name.tag or data['variant'],
            'nbench': data['nbench'],
            'host': name.host or data['hostname'],
            'cpu': data['cpu_model'],
            'suite_version': data['suite_version'],
            'date': date.strftime('%Y-%m-%d %H:%M'),
            'mtime': int(st.st_mtime),
            'size': formatFileSize(st.st_size),
            'bytes': st.st_size,
            'bad': data['fmt'] == 'bad',
            'build_number': (build or {}).get('number', data['build_number']),
            'build_url': ('%s/builds/%s' % (
                urllib.quote(build['builder']), build['number'])
                if build else ''),
            'configure_args': data['configure_args'],
        }
