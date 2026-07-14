import os
import py
from twisted.web.static import getTypeAndEncoding
from buildbot.status import builder as status_builder

from pypybuildbot import summary
from pypybuildbot.pypylist import PyPyTarball, PyPyList, ReleaseList, \
    PyPyDirectoryLister
from pypybuildbot.test.test_summary import FakeMaster, add_builds

def test_pypytarball_svn():
    t = PyPyTarball('pypy-c-jit-75654-linux.tar.bz2', '.')
    assert t.filename == 'pypy-c-jit-75654-linux.tar.bz2'
    assert t.exe == 'pypy'
    assert t.backend == 'c'
    assert t.features == 'jit'
    assert t.rev == '75654'
    assert t.numrev == 75654
    assert t.platform == 'linux'
    assert t.vcs == 'svn'


def test_pypytarball_hg():
    t = PyPyTarball('pypy-c-jit-75654-foo-linux.tar.bz2', '.')
    assert t.filename == 'pypy-c-jit-75654-foo-linux.tar.bz2'
    assert t.exe == 'pypy'
    assert t.backend == 'c'
    assert t.features == 'jit'
    assert t.rev == '75654:foo'
    assert t.numrev == 75654
    assert t.platform == 'linux'
    assert t.vcs == 'hg'


def test_invalid_filename():
    t = PyPyTarball('foo', '.')
    assert t.vcs == None
    assert t.filename == 'foo'
    assert t.exe == None
    assert t.backend == None
    assert t.features == None
    assert t.rev == -1
    assert t.platform == None
    t2 = PyPyTarball('pypy-c-jit-75654-linux.tar.bz2', '.')
    assert t.key() < t2.key()

def test_sort(tmpdir):
    files = [
            'pypy-c-jit-10000-linux.tar.bz2',
            'pypy-c-jit-20000-linux.tar.bz2',
            'pypy-c-nojit-10000-linux.tar.bz2',
            'pypy-c-jit-10000-linux64.tar.bz2',
            'pypy-c-jit-10000-win32.tar.bz2',
            'pypy-c-stackless-10000-linux.tar.bz2',
            'pypy-c-jit-1000-e5b73981fc8d-linux.tar.bz2', # this is mercurial based
            'pypy-c-jit-10000-linux-armel.tar.bz2',
            ]
    [tmpdir.join(f).write(f) for f in files]
    pypylist = PyPyList(tmpdir.strpath)
    listener = pypylist.directoryListing()
    assert listener.dirs == [
        'pypy-c-jit-1000-e5b73981fc8d-linux.tar.bz2', # mercurial first
        'pypy-c-jit-20000-linux.tar.bz2',
        'pypy-c-jit-10000-linux64.tar.bz2',
        'pypy-c-jit-10000-linux.tar.bz2',
        'pypy-c-jit-10000-win32.tar.bz2',
        'pypy-c-jit-10000-linux-armel.tar.bz2',
        'pypy-c-nojit-10000-linux.tar.bz2',
        'pypy-c-stackless-10000-linux.tar.bz2',
        ]

def test_pypy_list(tmpdir):
    import os
    pypylist = PyPyList(os.path.dirname(__file__))
    files = pypylist.listNames()
    assert os.path.basename(__file__) in files


def test_release_archives_are_octet_streams(tmpdir):
    releases = ReleaseList(tmpdir.strpath)
    for filename in [
        'pypy3.11-v7.3.23-linux64.tar.bz2',
        'pypy3.11-v7.3.23-linux64.tar.gz',
    ]:
        tmpdir.join(filename).write('archive contents')
        release = releases.getChild(filename, None)
        assert release.type == 'application/octet-stream'
        assert release.encoding is None


def test_other_compressed_release_files_keep_their_content_encoding(tmpdir):
    releases = ReleaseList(tmpdir.strpath)
    for filename, expected_encoding in [
        ('release-notes.txt.bz2', 'bzip2'),
        ('release-notes.txt.gz', 'gzip'),
    ]:
        tmpdir.join(filename).write('compressed contents')
        release = releases.getChild(filename, None)
        assert release.type is None
        content_type, content_encoding = getTypeAndEncoding(
            filename,
            release.contentTypes,
            release.contentEncodings,
            release.defaultType,
        )
        assert content_type == 'text/plain'
        assert content_encoding == expected_encoding

def test_dir_render(tmpdir):
    # Create a bunch of directories, including one named trunk,
    # Make sure the time order is reversed collation order
    trunk = tmpdir.mkdir('trunk')
    oldtime = trunk.mtime()
    for ascii in range(ord('a'), ord('m')):
        newdir = tmpdir.mkdir(chr(ascii) * 4)
        newdir.setmtime(oldtime + ascii * 10)
    pypylist = PyPyList(tmpdir.strpath)
    listener = pypylist.directoryListing()
    assert listener.dirs == ['trunk', 'llll',
        'kkkk','jjjj','iiii','hhhh','gggg','ffff','eeee',
        'dddd','cccc','bbbb','aaaa']

def load_BuildmasterConfig():
    import os
    from pypybuildbot import summary, builds, arm_master
    def load(name):
        if name == 'pypybuildbot.summary':
            return summary
        elif name == 'pypybuildbot.builds':
            return builds
        elif name == 'pypybuildbot.arm_master':
            return arm_master
        else:
            assert False

    this = py.path.local(__file__)
    master_py = this.dirpath().dirpath().join('master.py')
    glob = {'httpPortNumber': 80,
            'slavePortnum': 1234,
            'passwords': {},
            # master.cfg injects these into master.py's namespace before
            # execfile'ing it; mirror that here so the config loads.
            'upload_credentials': None,
            'load': load,
            'os': os}
    execfile(str(master_py), glob)
    return glob['BuildmasterConfig']

def test_builder_names():
    BuildmasterConfig = load_BuildmasterConfig()
    builders = [b['name'] for b in BuildmasterConfig['builders']]
    known_exceptions = set(['pypy-c-jit-macos-x86-64'])
    def check_builder_names(t, expected_own, expected_app):
        own, app = t.get_builder_names()
        assert own == expected_own
        assert app == expected_app
        assert own in builders or own in known_exceptions
        assert app in builders or app in known_exceptions

    t = PyPyTarball('pypy-c-jit-76867-linux.tar.bz2', '.')
    check_builder_names(t, 'own-linux-x86-32', 'pypy-c-jit-linux-x86-32')

    t = PyPyTarball('pypy-c-jit-76867-macos_x86_64.tar.bz2', '.')
    check_builder_names(t, 'own-macos-x86-64', 'pypy-c-jit-macos-x86-64')

    t = PyPyTarball('pypy-c-jit-76867-linux64.tar.bz2', '.')
    check_builder_names(t, 'own-linux-x86-64', 'pypy-c-jit-linux-x86-64')

    t = PyPyTarball('pypy-c-jit-76867-win64.tar.bz2', '.')
    check_builder_names(t, 'own-win-x86-64', 'pypy-c-jit-win-x86-64')


def _status_with_builds(builder_name, builds_list, category=None):
    # build a real buildbot Status over fake builders that carry finished
    # builds with pytest logs, mirroring test_summary's setup
    summary.outcome_set_cache.clear()
    master = FakeMaster([])
    builder = status_builder.BuilderStatus(builder_name, category, master, '')
    add_builds(builder, builds_list)
    return status_builder.Status(FakeMaster([builder]))


def test_revision_summaries_from_text_log():
    status = _status_with_builds('own-linux-x86-64',
        [('12345:abcdef',
          "F a/b.py:test_one\n. a/b.py:test_two\ns a/c.py:test_three\n")],
        category='linux64')
    summaries, category = summary.revision_summaries(status, 'own-linux-x86-64')
    assert category == 'linux64'
    assert summaries[('main', '12345:abcdef')].to_tuple() == (1, 1, 1, 0)


def test_revision_summaries_from_xml_log():
    # junitxml results must produce the same summary the summary page shows;
    # the old build-time cache (populate() only) could not parse xml at all.
    xml = open(os.path.join(os.path.dirname(__file__), 'log.xml')).read()
    status = _status_with_builds('own-linux-x86-64', [('999:cafe', xml)])
    summaries, _ = summary.revision_summaries(status, 'own-linux-x86-64')
    assert summaries[('main', '999:cafe')].to_tuple() == (12, 2, 1, 0)


def test_lister_reads_results_from_logs():
    status = _status_with_builds('own-linux-x86-64',
        [('12345:abcdef', "F a/b.py:test_one\n. a/b.py:test_two\n")],
        category='linux64')
    lister = PyPyDirectoryLister('.')
    lister.status = status
    lister._summaries_cache = {}

    s, cat = lister._get_summary_and_category(
        'own-linux-x86-64', 'main', '12345:abcdef')
    assert str(s) == '1, 1 F, 0 s, 0 x'
    assert cat == 'linux64'

    # a revision with no build -> None (renders blank on the page)
    s2, _ = lister._get_summary_and_category('own-linux-x86-64', 'main', 'nope')
    assert s2 is None

    # results for an unknown builder -> None, not an exception
    s3, cat3 = lister._get_summary_and_category('no-such-builder', 'main', 'x')
    assert (s3, cat3) == (None, None)

    # the builder's logs are scanned only once per render (memoized)
    assert 'own-linux-x86-64' in lister._summaries_cache
