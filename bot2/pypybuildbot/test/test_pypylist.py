import py
from twisted.web.static import getTypeAndEncoding

from pypybuildbot.pypylist import PyPyTarball, PyPyList, ReleaseList

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

    t = PyPyTarball('pypy-c-nojit-76867-linux.tar.bz2', '.')
    check_builder_names(t, 'own-linux-x86-32', 'pypy-c-app-level-linux-x86-32')

    t = PyPyTarball('pypy-c-jit-76867-macos_x86_64.tar.bz2', '.')
    check_builder_names(t, 'own-macos-x86-64', 'pypy-c-jit-macos-x86-64')

    t = PyPyTarball('pypy-c-jit-76867-linux64.tar.bz2', '.')
    check_builder_names(t, 'own-linux-x86-64', 'pypy-c-jit-linux-x86-64')

    t = PyPyTarball('pypy-c-jit-76867-win32.tar.bz2', '.')
    check_builder_names(t, 'own-win-x86-32', 'pypy-c-jit-win-x86-32')

    t = PyPyTarball('pypy-c-nojit-76867-linux64.tar.bz2', '.')
    check_builder_names(t, 'own-linux-x86-64', 'pypy-c-app-level-linux-x86-64')
