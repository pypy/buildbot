import py
from pypybuildbot import builds

class FakeProperties(object):

    sources = {}

    def __init__(self, properties=None):
        if properties is None:
            self.properties = {'branch':None, 'got_revision': 123,
                    'final_file_name': '123-ea5ca8'}
        else:
            self.properties = properties

    def __getitem__(self, item):
        return self.properties.get(item)

    def __setitem__(self, name, value):
        self.properties[name] = value

    def has_key(self, name):
        return name in self.properties

    def render(self, x):
        return x

class FakeSourceStamp(object):
    def __init__(self, properties=None):
        self.properties = properties if properties is not None else {}

    def __getattr__(self, name):
        return self.properties.get(name)

    def __setattribute__(self, name, value):
        self.properties[name] = value

class FakeBuild(object):
    slaveEnvironment = None
    master = None

    def __init__(self, properties=None):
        self.properties = FakeProperties(properties)
        self.source_stamp = FakeSourceStamp(properties)

    def getProperties(self):
        return self.properties

    def setProperty(self, name, value, source):
        self.properties[name] = value
        self.properties.sources[name] = source

    def getSlaveCommandVersion(self, *args):
        return 3

    def getSourceStamp(self, *args):
        return self.source_stamp

class FakeStepStatus(object):
    def setText(self, *args):
        pass

    def stepFinished(self, results):
        self.results = results

    def setHidden(self, *args):
        pass

class FakeDeferred(object):
    def callback(*args):
        pass
    def addCallback(self, *args):
        return FakeDeferred()
    def addErrback(self, *args):
        return FakeDeferred()

def test_Translate():
    expected = ['pypy', '../../rpython/bin/rpython', '--batch', '-O0',
                'targetpypystandalone', '--no-allworkingmodules']

    translateInst = builds.Translate(['-O0'], ['--no-allworkingmodules'])

    assert translateInst.command[-len(expected):] == expected
    
    translateFactory = translateInst._getStepFactory().factory
    args = translateInst._getStepFactory().args
    rebuiltTranslate = translateFactory(*args)
                
    assert rebuiltTranslate.command[-len(expected):] == expected

    rebuiltTranslate.build = FakeBuild()
    rebuiltTranslate.setBuild(rebuiltTranslate.build)
    rebuiltTranslate.startCommand = lambda *args: None
    rebuiltTranslate.start()

def test_pypy_upload():
    pth = py.test.ensuretemp('buildbot')
    inst = builds.PyPyUpload(slavesrc='slavesrc', masterdest=str(pth.join('mstr')),
                             basename='base-%(final_file_name)s', workdir='.',
                             blocksize=100)
    factory = inst._getStepFactory().factory
    kw = inst._getStepFactory().kwargs
    rebuilt = factory(**kw)
    rebuilt.build = FakeBuild()
    rebuilt.setStepStatus(FakeStepStatus())
    rebuilt.runCommand = lambda *args: FakeDeferred()
    rebuilt.start()
    assert pth.join('mstr').check(dir=True)
    assert rebuilt.masterdest == str(pth.join('mstr', 'main',
                                              'base-123-ea5ca8'))
    assert rebuilt.symlinkname == str(pth.join('mstr', 'main',
                                               'base-latest'))

def _run_upload_with_extension(properties):
    pth = py.test.ensuretemp('buildbot')
    # basename carries the archive extension as a build-time property, with a
    # fallback default, exactly as the Translated/NightlyBuild factories build it
    inst = builds.PyPyUpload(slavesrc='slavesrc', masterdest=str(pth.join('mstr')),
                             basename='base-%(final_file_name)s%(extension:~.tar.bz2)s',
                             workdir='.', blocksize=100)
    factory = inst._getStepFactory().factory
    kw = inst._getStepFactory().kwargs
    rebuilt = factory(**kw)
    rebuilt.build = FakeBuild(properties)
    rebuilt.setStepStatus(FakeStepStatus())
    rebuilt.runCommand = lambda *args: FakeDeferred()
    rebuilt.start()
    return rebuilt

def test_pypy_upload_extension_from_property():
    # the worker reported an 'extension' property; it must win over the default
    rebuilt = _run_upload_with_extension(
        {'branch': None, 'final_file_name': '123-ea5ca8', 'extension': '.tar.gz'})
    pth = py.test.ensuretemp('buildbot')
    assert rebuilt.masterdest == str(pth.join('mstr', 'main',
                                              'base-123-ea5ca8.tar.gz'))
    assert rebuilt.symlinkname == str(pth.join('mstr', 'main',
                                               'base-latest.tar.gz'))

def test_pypy_upload_extension_default():
    # no 'extension' property -> fall back to the platform default in the fragment
    rebuilt = _run_upload_with_extension(
        {'branch': None, 'final_file_name': '123-ea5ca8'})
    pth = py.test.ensuretemp('buildbot')
    assert rebuilt.masterdest == str(pth.join('mstr', 'main',
                                              'base-123-ea5ca8.tar.bz2'))
    assert rebuilt.symlinkname == str(pth.join('mstr', 'main',
                                               'base-latest.tar.bz2'))

class TestPytestCmd(object):

    class Fake(object):
        def __init__(self, **kwds):
            self.__dict__.update(kwds)

    class FakeBuildStatus(Fake):
        def getProperties(self):
            return self.properties

    class FakeBuilder(Fake):
        def saveYourself(self):
            pass

    class FakeLog(object):
        # a status log exposes both getText() (used to detect junitxml) and
        # readlines() (used by the resultlog parser)
        def __init__(self, text):
            self.text = text
        def getText(self):
            return self.text
        def readlines(self):
            return [l + '\n' for l in self.text.splitlines()]

    def _make(self, log, properties):
        step = builds.PytestCmd()
        step.build = self.Fake()
        step.build.build_status = self.FakeBuildStatus(properties=properties)
        step.build.build_status.builder = builder = self.FakeBuilder()
        cmd = self.Fake(logs={'pytestLog': self.FakeLog(log)})
        return step, cmd, builder

    def _create(self, log, rev, branch):
        return self._make(log, {'got_revision': rev, 'branch': branch})

    def test_no_log(self):
        step = builds.PytestCmd()
        cmd = self.Fake(logs={})
        assert step.commandComplete(cmd) is None

    def test_empty_log(self):
        step, cmd, builder = self._create(log='', rev='123', branch='trunk')
        step.commandComplete(cmd)
        summary = builder.summary_by_branch_and_revision[('trunk', '123')]
        assert summary.to_tuple() == (0, 0, 0, 0)

    def test_summary(self):
        log = """F a/b.py:test_one
. a/b.py:test_two
s a/b.py:test_three
S a/c.py:test_four
"""
        step, cmd, builder = self._create(log=log, rev='123', branch='trunk')
        step.commandComplete(cmd)
        summary = builder.summary_by_branch_and_revision[('trunk', '123')]
        assert summary.to_tuple() == (1, 1, 2, 0)

    def test_branch_is_None(self):
        step, cmd, builder = self._create(log='', rev='123', branch=None)
        step.commandComplete(cmd)
        assert ('main', '123') in builder.summary_by_branch_and_revision

    def test_trailing_slash(self):
        step, cmd, builder = self._create(log='', rev='123', branch='branch/foo/')
        step.commandComplete(cmd)
        assert ('branch/foo', '123') in builder.summary_by_branch_and_revision

    def test_missing_branch_property_still_records(self):
        # a nightly/forced trigger may set no 'branch' property; buildbot's
        # real Properties raises KeyError on the missing key.  The result must
        # still be recorded (under the default branch 'main'), else the nightly
        # page shows None though the summary page shows the results.
        step, cmd, builder = self._make(
            '. a/b.py:test_one\n', {'got_revision': '171295:eb76a8b4666f'})
        step.commandComplete(cmd)
        assert ('main', '171295:eb76a8b4666f') in \
            builder.summary_by_branch_and_revision

    def test_xml_log(self):
        # junitxml logs must be parsed like the summary page (populate_xml),
        # not fed to the resultlog parser, which would produce garbage failures
        import os
        xml = open(os.path.join(os.path.dirname(__file__), 'log.xml')).read()
        step, cmd, builder = self._create(log=xml, rev='123', branch='main')
        step.commandComplete(cmd)
        summary = builder.summary_by_branch_and_revision[('main', '123')]
        assert summary.to_tuple() == (12, 2, 1, 0)

    def test_multiple_logs(self):
        log = """F a/b.py:test_one
. a/b.py:test_two
s a/b.py:test_three
S a/c.py:test_four
"""
        step, cmd, builder = self._create(log=log, rev='123', branch='trunk')
        step.commandComplete(cmd)
        step.commandComplete(cmd)
        summary = builder.summary_by_branch_and_revision[('trunk', '123')]
        assert summary.to_tuple() == (2, 2, 4, 0)


class TestParseRevision(object):

    def setup_method(self, mth):
        inst = builds.ParseRevision()
        factory = inst._getStepFactory().factory
        kw = inst._getStepFactory().kwargs
        self.rebuilt = factory(**kw)
        self.rebuilt.setStepStatus(FakeStepStatus())
        self.rebuilt.deferred = FakeDeferred()

    def test_has_revision(self):
        self.rebuilt.build = FakeBuild({'revision':u'123:ea5ca8'})
        self.rebuilt.start()
        assert self.rebuilt.build.getProperties()['revision'] == 'ea5ca8'
        assert self.rebuilt.build.getProperties()['original_revision'] == '123:ea5ca8'
        assert self.rebuilt.build.getProperties()['final_file_name'] == '123-ea5ca8'

    def test_no_revision(self):
        self.rebuilt.build = FakeBuild()
        self.rebuilt.start()
        assert self.rebuilt.build.getProperties()['revision'] is None

    def test_revision_no_local_part(self):
        self.rebuilt.build = FakeBuild({'revision':u'ea5ca8'})
        self.rebuilt.start()
        assert self.rebuilt.build.getProperties()['revision'] == 'ea5ca8'

    def test_empty_revision(self):
        self.rebuilt.build = FakeBuild({'revision':u''})
        self.rebuilt.start()
        assert self.rebuilt.build.getProperties()['revision'] == ''
