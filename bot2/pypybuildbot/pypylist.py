import os.path
import datetime
import itertools
import collections
import py
import cgi
import urllib
import sys
from twisted.web.static import File, formatFileSize
from twisted.python import log
from buildbot.status.web.base import DirectoryLister, ContextMixin

class PyPyTarball(object):

    # to get the desired order keep in mind that they are reversed at the end,
    # so the highest the value, the bigger the priority
    VCS_PRIORITY = {
        'latest': 150,
        'hg': 100,
        'svn': 50,
        }

    FEATURES_PRIORITY = {
        'jit':      100,
        'nojit':     50,
        'stackless': 10
        }

    PLATFORM_PRIORITY = {
        'linux64':  100,
        'linux':     50,
        'aarch64':   40,
        'macos_arm64': 31,
        'macos_86_64': 30,
        'win64':     21,
        'win32':     20,
        's390x':     10,
        'linux_armhf_raspbian': 7,
        'linux_armhf_raring': 6,
        'linux_armel': 5,
        }

    PLATFORMS = {
        'linux':     'linux-x86-32',
        'linux64':   'linux-x86-64',
        'aarch64':   'linux-aarch64',
        'macos_x86_64': 'macos-x86-64',
        'macos_arm64': 'macos-arm64',
        'win32':     'win-x86-32',
        'win64':     'win-x86-64',
        'freebsd64': 'freebsd-9-x86-64',
        's390x':     'linux-s390x',
        }

    DESCRIPTIONS = {
        'nojit':     'app-level',
        'jit':       'jit',
        'stackless': 'stackless-app-level',
        }

    def __init__(self, filename, dirname):
        self.filename = filename
        try:
            self.parse_filename()
        except ValueError:
            self.vcs = None
            self.exe = None
            self.backend = None
            self.features = None
            self.rev = -1
            self.numrev = -1
            self.platform = None
        dd = py.path.local(dirname).join(filename)
        if dd.exists():
            self.date = datetime.date.fromtimestamp(dd.mtime())
        else:
            self.date = datetime.date(2000, 1, 1)

    def parse_filename(self):
        for ext in ['.tar.bz2', '.zip']:
            if self.filename.endswith(ext):
                break
        else:
            raise ValueError
        name = self.filename.replace(ext, '')
        # remove the dash from linux-armel, else the split does not work
        name = name.replace('-armel', '_armel')
        name = name.replace('-libc2', '_libc2')
        name = name.replace('-armhf-ra', '_armhf_ra')
        dashes = name.count('-')
        if dashes == 4:
            # svn based
            self.exe, self.backend, self.features, self.rev, self.platform = name.split('-')
            if self.rev == 'latest':
                self.rev = -1
                self.numrev = -1
                self.vcs = 'latest'
            else:
                self.numrev = int(self.rev)
                self.vcs = 'svn'
        elif dashes == 5:
            # mercurial based
            self.exe, self.backend, self.features, num, hgid, self.platform = name.split('-')
            self.numrev = int(num)
            self.rev = '%s:%s' % (num, hgid)
            self.vcs = 'hg'
        else:
            raise ValueError

    def key(self):
        return (self.VCS_PRIORITY.get(self.vcs, -1),
                self.date,
                self.numrev,
                self.FEATURES_PRIORITY.get(self.features, -1),
                self.PLATFORM_PRIORITY.get(self.platform, -1))

    def get_builder_names(self):
        platform = self.PLATFORMS.get(self.platform, self.platform)
        description = self.DESCRIPTIONS.get(self.features, self.features)
        own_builder = 'own-%s' % platform
        app_builder = '%s-%s-%s-%s' % (self.exe, self.backend, description, platform)
        return own_builder, app_builder

    def display_in_italic(self):
        return self.vcs == 'latest'

class PyPyDirectory(object):
    def __init__(self, filePath):
        self.filename = filePath.basename()
        self.filePath = filePath
        self.parse_filename()

    def parse_filename(self):
        if self.filename in ('trunk', 'main'):
            self.last_mod_time = sys.maxsize
            return
        self.last_mod_time = self.filePath.getModificationTime()

    def key(self):
        return (self.last_mod_time)

class PyPyList(File):

    def sortBuildNames(self, names, dirname):
        items = [PyPyTarball(name, dirname) for name in names]
        items.sort(key=PyPyTarball.key, reverse=True)
        return [item.filename for item in items]

    def sortDirectoryNames(self, filePaths):
        items = map(PyPyDirectory, filePaths)
        items.sort(key=PyPyDirectory.key, reverse=True)
        return [item.filename for item in items]

    def directoryListing(self):
        def is_pypy_dir(names):
            for name in names:
                if name.startswith('pypy-c'):
                    return True
            return False
        names = File.listNames(self)
        if is_pypy_dir(names):
            names = self.sortBuildNames(names, self.path)
        else:
            names = self.sortDirectoryNames(File.listEntities(self))
        Listener = PyPyDirectoryLister
        return Listener(self.path,
                        names,
                        self.contentTypes,
                        self.contentEncodings,
                        self.defaultType)

class PyPyDirectoryLister(DirectoryLister):
    '''template based, uses master/templates/directory.html
    '''

    def render(self, request):
        self.status = request.site.buildbot_service.getStatus()
        return DirectoryLister.render(self, request)

    def _getFilesAndDirectories(self, directory):
        dirs, files = DirectoryLister._getFilesAndDirectories(self, directory)
        rowClasses = itertools.cycle(['odd', 'even'])
        for f, rowClass in zip(files, rowClasses):
            f["class"] = rowClass
            self._add_test_results(f, rowClass)
        for d in dirs:
            dirname = urllib.unquote(d['href'])
            dd = py.path.local(self.path).join(dirname)
            date = datetime.date.fromtimestamp(dd.mtime())
            d['date'] = date.isoformat()
            # Assume dir is non-recursive
            size = sum([f.size() for f in dd.listdir() if f.isfile()])
            d['size'] = formatFileSize(size)

        return dirs, files

    def _add_test_results(self, element, rowClass):
        filename = urllib.unquote(element['href'])
        f = py.path.local(self.path).join(filename)
        date = datetime.date.fromtimestamp(f.mtime())
        element['date'] = date.isoformat()
        t = PyPyTarball(filename, self.path)
        if t.display_in_italic():
            element['text'] = '<i>%s</i>' % (element['text'],)
        own_builder, app_builder = t.get_builder_names()
        self._add_result_for_builder(element, own_builder, 'own_', t.rev, rowClass)
        self._add_result_for_builder(element, app_builder, 'app_', t.rev, rowClass)

    def _add_result_for_builder(self, element, builder_name, prefix, rev, rowClass):
        if rev == -1:
            summary = None
            str_summary = ''
        else:
            branch = self._get_branch()
            summary, category = self._get_summary_and_category(builder_name, branch, rev)
            if branch == 'trunk':
                branch = '%3Ctrunk%3E' # <trunk>
            if category:
                href = cgi.escape('/summary?category=%s&branch=%s&recentrev=%s' % (category, branch, rev))
                str_summary = '<a class="summary_link" href="%s">%s</a>' % (href, summary)
            else:
                str_summary = str(summary)
        element[prefix + 'summary'] = str_summary
        element[prefix + 'summary_class'] = self._get_summary_class(summary, rowClass)

    def _get_branch(self):
        parts = self.path.split(os.path.sep)
        i = parts.index('nightly')
        branch = os.path.sep.join(parts[i+1:])
        return branch

    def _get_summary_and_category(self, builder_name, branch, rev):
        try:
            builder = self.status.getBuilder(builder_name)
            return builder.summary_by_branch_and_revision[(branch, rev)], builder.category
        except (AttributeError, KeyError):
            return None, None
            # for testing
            ## from pypybuildbot.summary import OutcomeSummary
            ## import random
            ## if random.choice([True, True, True, False]):
            ##     return OutcomeSummary(1000, 0, 2, 4), None
            ## else:
            ##     return OutcomeSummary(990, 10, 2, 4), None

    def _get_summary_class(self, summary, rowClass):
        if summary is None:
            return rowClass
        elif summary.is_ok():
            return rowClass + '-passed'
        else:
            return rowClass + '-failed'

class NumpyStatusList(PyPyList):
    pass


class ReleaseList(File):
    archiveSuffixes = ('.tar.bz2', '.tar.gz')

    def createSimilarFile(self, path):
        child = File.createSimilarFile(self, path)
        if path.lower().endswith(self.archiveSuffixes):
            # Twisted's File treats .gz and .bz2 as pre-compressed variants of
            # another resource by default.  It uses the preceding suffix for
            # Content-Type and sends the compression suffix as
            # Content-Encoding.
            # For these downloadable tar archives compression is part of the
            # file format.  A content encoding may make clients transparently
            # decompress and change the downloaded bytes.
            child.type = 'application/octet-stream'
            child.encoding = None
        return child

    def directoryListing(self):
        return ReleaseLister(self.path,
                                self.listNames(),
                                self.contentTypes,
                                self.contentEncodings,
                                self.defaultType)

class ReleaseLister(DirectoryLister, ContextMixin):
    """This variant of the static.DirectoryLister uses a template
    for rendering."""

    pageTitle = 'PyPy Releases'

    def render(self, request):
        cxt = self.getContext(request)
        # Keep stylesheet under /pypy so it is picked up by
        # downloads.python.org/pypy. This means you must copy it to
        # public_html/mirror
        cxt['path_to_root'] = '/pypy/'
        cxt['stylesheet'] = '/pypy/default.css'

        if self.dirs is None:
            directory = os.listdir(self.path)
            directory.sort()
        else:
            directory = self.dirs

        dirs, files = self._getFilesAndDirectories(directory)
        releases = {}
        releases['other'] = []
        for f in files:
            fname = urllib.unquote(f['href'])
            dd = py.path.local(self.path).join(fname)
            date = datetime.date.fromtimestamp(dd.mtime())
            f['date'] = date.isoformat()
            f['rc'] = 0
            # Assume dir is non-recursive
            v = f['text'].split('-')
            if v[0].startswith('pypy') and len(v) > 2:
                python, pypy = v[:2]
                indx = pypy.find('rc')
                if indx > 0:
                    pypy = pypy[:indx]
                    f['rc'] = 1
                pypy = pypy.replace('+', '')
                python = python.replace('pypy', '')
                if pypy in releases:
                    releases[pypy][python].append(f)
                else:
                    releases[pypy] = collections.defaultdict(list)
                    releases[pypy][python] = [f]
            else:
                releases['other'].append(f)
                    
        # build a sorted list of the releases
        # first, the ones that start with 'v', then the rest
        def f(a):
            #          major, minor, rev, rc
            if a == 'other':
                return 0
            factors = [40000 ,1000,  20,  1]
            vals = a.split('.')
            if len(vals) < 3:
                # not a version number
                return 1
            if a[0] == 'v':
                vals[0] = vals[0][1:]
                base = int(1e6)
            else:
                base = 0
            base += sum([int('0' + x) * y for x, y in zip(vals, factors)])
            return base
        
        cxt['path'] = cgi.escape(urllib.unquote(request.uri))
        cxt['directories'] = dirs
        cxt['releases'] = releases
        cxt['headings'] = sorted(releases.keys(), key=f, reverse=True)
        template = request.site.buildbot_service.templates.get_template("release.html")
        data = template.render(**cxt)
        if isinstance(data, unicode):
            data = data.encode("utf-8")
        return data


