from buildbot.steps.source.mercurial import Mercurial
from buildbot.steps.source.git import Git
from buildbot.process.buildstep import BuildStep
from buildbot.process import factory
from buildbot.steps import shell, transfer
from buildbot.steps.trigger import Trigger
from buildbot.process.properties import WithProperties, Interpolate, Property, renderer
from buildbot import locks
from pypybuildbot.util import symlink_force
from buildbot.status.results import SKIPPED, SUCCESS
import glob
import os
import json

# buildbot supports SlaveLocks, which can be used to limit the amout of builds
# to be run on each slave in parallel.  However, they assume that each
# buildslave is on a differen physical machine, which is not the case for
# bencher4 and bencher4_32.  As a result, we have to use a global lock, and
# manually tell each builder that uses benchmarker to acquire it.
#
# Look at the various "locks" session in master.py/BuildmasterConfig.  For
# benchmarks, the locks is aquired for the single steps: this way we can run
# translations in parallel, but then the actual benchmarks are run in
# sequence.

# benchmarker has 8 logical CPUs, but only 4 physical ones, and memory for ~6 translations
BenchmarkerLock = locks.MasterLock('benchmarker', maxCount=3)
Benchmarker2Lock = locks.MasterLock('benchmarker2', maxCount=3)
SpeedPythonCPU = locks.MasterLock('speed_python_cpu', maxCount=24)
WinSlaveLock = locks.SlaveLock('win_cpu', maxCount=2)
# speed-old has 24 cores, but memory for ~2 translations
#SpeedOldLock = locks.MasterLock('speed_old_lock', maxCount=2)
# bencher4 has 8 cores, 32 GB RAM
Bencher4Lock = locks.MasterLock('bencher4_lock', maxCount=1)
AARCH64Lock = locks.MasterLock('aarch64_lock', maxCount=1)

# The cross translation machine can accomodate 2 jobs at the same time
ARMCrossLock = locks.MasterLock('arm_cpu', maxCount=2)
# while the boards can only run one job at the same time
ARMBoardLock = locks.SlaveLock('arm_boards', maxCount=1)
Salsa_m1_lock = locks.SlaveLock('salsa-m1', maxCount=1)

map_branch_name = lambda x: 'main' if x in ['', None, 'default'] else x

class ShellCmd(shell.ShellCommand):
    # our own version that can distinguish abort cases (rc == -1)

    def getText(self, cmd, results):
        if cmd is not None and cmd.rc == -1:
            return self.describe(True) + ['aborted']
        return shell.ShellCommand.getText(self, cmd, results)


class PyPyUpload(transfer.FileUpload):
    parms = transfer.FileUpload.parms + ['basename']
    haltOnFailure = False

    def start(self):
        properties = self.build.getProperties()
        branch = map_branch_name(properties['branch'])
        #masterdest = properties.render(self.masterdest)
        masterdest = os.path.expanduser(self.masterdest)
        if branch.startswith('/'):
            branch = branch[1:]
        # workaround for os.path.join
        masterdest = os.path.join(masterdest, branch)
        if not os.path.exists(masterdest):
            os.makedirs(masterdest)
        #
        assert '%(final_file_name)s' in self.basename
        symname = self.basename.replace('%(final_file_name)s', 'latest')
        symname = WithProperties(symname).getRenderingFor(self.build)
        assert '%' not in symname
        self.symlinkname = os.path.join(masterdest, symname)
        #
        basename = WithProperties(self.basename).getRenderingFor(self.build)
        self.masterdest = os.path.join(masterdest, basename)
        #
        transfer.FileUpload.start(self)

    def finished(self, *args, **kwds):
        transfer.FileUpload.finished(self, *args, **kwds)
        try:
            os.chmod(self.masterdest, 0644)
        except OSError:
            pass
        if os.stat(self.masterdest).st_size > 10:
            try:
                symlink_force(os.path.basename(self.masterdest), self.symlinkname)
            except OSError:
                pass

class PyPyDownload(transfer.FileDownload):
    parms = transfer.FileDownload.parms + ['basename']

    def start(self):

        properties = self.build.getProperties()
        branch = map_branch_name(properties['branch'])
        revision = properties.getProperty('final_file_name')
        mastersrc = os.path.expanduser(self.mastersrc)

        if branch.startswith('/'):
            branch = branch[1:]
        mastersrc = os.path.join(mastersrc, branch)
        if revision:
            basename = WithProperties(self.basename).getRenderingFor(self.build)
            basename = basename.replace(':', '-')
        else:
            basename = self.basename.replace('%(final_file_name)s', 'latest')
            basename = WithProperties(basename).getRenderingFor(self.build)
            assert '%' not in basename

        self.mastersrc = os.path.join(mastersrc, basename)
        #
        transfer.FileDownload.start(self)

class NumpyStatusUpload(transfer.FileUpload):
    def finished(self, *args, **kwds):
        transfer.FileUpload.finished(self, *args, **kwds)
        try:
            os.chmod(self.masterdest, 0644)
        except OSError:
            pass
        try:
            symname = os.path.join(os.path.dirname(self.masterdest),
                                   'latest.html')
            symlink_force(self.masterdest, symname)
        except OSError:
            pass

class Translate(ShellCmd):
    name = "translate"
    description = ["translating"]
    descriptionDone = ["translation"]

    command = ["../../rpython/bin/rpython", "--batch"]
    translationTarget = "targetpypystandalone"
    haltOnFailure = True

    def __init__(self, translationArgs, targetArgs,
                 workdir="build/pypy/goal",
                 interpreter='pypy',
                 *a, **kw):
        add_args = {'translationArgs': translationArgs,
                    'targetArgs': targetArgs,
                    'interpreter': interpreter}
        kw['timeout'] = 7200
        ShellCmd.__init__(self, workdir, *a, **kw)
        self.addFactoryArguments(**add_args)
        self.command = ([interpreter] + self.command + translationArgs +
                        [self.translationTarget] + targetArgs)
        #self.command = ['cp', '/tmp/pypy-c', '.']


class PytestCmd(ShellCmd):
    # A pytest step whose result log (see logfiles={'pytestLog': ...}) is the
    # single source of truth for test results.  The summary page and the
    # nightly download page both parse it on demand via pypybuildbot.summary
    # (RevisionOutcomeSet / revision_summaries); nothing is cached at build
    # time.
    pass

class SuccessAlways(ShellCmd):
    def evaluateCommand(self, cmd):
        return SUCCESS

# _______________________________________________________________
# XXX Currently the build properties got_revision and final_file_name contain
# the revision number and the changeset-id, CheckGotRevision takes care to set
# the corresponding build properties
# rev:changeset for got_revision
# rev-changeset for final_file_name
#
# The rev part of got_revision and filename is used everywhere to sort the
# builds, i.e. on the summary and download pages.
#
# The rev part is strictly local and needs to be removed from the SourceStamp,
# at least for decoupled builds, which is what ParseRevision does.
#
# XXX in general it would be nice to drop the revision-number using only the
# changeset-id for got_revision and final_file_name and sorting the builds
# chronologically

class UpdateGitCheckout(ShellCmd):
    description = 'git checkout'
    command = 'UNKNOWN'

    def __init__(self, workdir=None, haltOnFailure=True, force_branch=None,
                 **kwargs):
        ShellCmd.__init__(self, workdir=workdir, haltOnFailure=haltOnFailure,
                          **kwargs)
        self.force_branch = force_branch
        self.addFactoryArguments(force_branch=force_branch)

    def start(self):
        if self.force_branch is not None:
            branch = self.force_branch
            # Note: We could add a warning to the output if we
            # ignore the branch set by the user.
        else:
            properties = self.build.getProperties()
            branch = properties['branch'] or 'main'
        command = ["git", "checkout", "-f", branch]
        self.setCommand(command)
        ShellCmd.start(self)


class CheckGotRevision(ShellCmd):
    description = 'got_revision'
    command = "git rev-list --count HEAD && git rev-parse --short=12 HEAD"

    def commandComplete(self, cmd):
        if cmd.rc == 0:
            got_revision = cmd.logs['stdio'].getText()
            got_revision = got_revision.replace("\n",":")
            # Prefix the revisions with 1 since the move to git so sorting
            # on the summary page still works
            got_revision = "1" + got_revision
            # manually get the effect of {node|short} without using a
            # '|' in the command-line, because it doesn't work on Windows
            #
            # This is a noop since the move to git
            num = got_revision.find(':')
            if num > 0:
                got_revision = got_revision[:num + 13]
            #
            final_file_name = got_revision.replace(':', '-')
            # ':' should not be part of filenames --- too many issues
            self.build.setProperty('got_revision', got_revision,
                                   'got_revision')
            if not self.build.hasProperty('final_file_name'):
                self.build.setProperty('final_file_name', final_file_name,
                                       'got_revision')

class ParseRevision(BuildStep):
    """Parse the revision property of the source stamp and extract the global
    part of the revision
    123:3a34 -> 3a34"""
    name = "parse_revision"

    def __init__(self, *args, **kwargs):
        BuildStep.__init__(self, *args, **kwargs)

    @staticmethod
    def hideStepIf(results, step):
        return results==SKIPPED

    @staticmethod
    def doStepIf(step):
        revision = step.build.getSourceStamp().revision
        return isinstance(revision, (unicode, str)) and ':' in revision

    def start(self):
        stamp = self.build.getSourceStamp()
        revision = stamp.revision if stamp.revision is not None else ''
        #
        if not isinstance(revision, (unicode, str)) or ":" not in revision:
            self.finished(SKIPPED)
            return
        #
        self.build.setProperty('original_revision', revision, 'parse_revision')
        self.build.setProperty('final_file_name',
                                revision.replace(':', '-'), 'parse_revision')
        #
        parts = revision.split(':')
        self.build.setProperty('revision', parts[1], 'parse_revision')
        stamp.revision = parts[1]
        self.finished(SUCCESS)


# hack the Mercurial class in-place: it should do "hg pull" without
# passing a "--rev" argument.  The problem is that while it sounds like
# a good idea, passing a "--rev" argument here changes the order of
# the checkouts.  Then our revisions "12345:432bcbb1ba" are bogus.
def _my_pullUpdate(self, res):
    command = ['pull', self.repourl]
    #if self.revision:                   <disabled!>
    #    command.extend(['--rev', self.revision])
    d = self._dovccmd(command)
    d.addCallback(self._checkBranchChange)
    return d
assert hasattr(Mercurial, '_pullUpdate')
Mercurial._pullUpdate = _my_pullUpdate


def update_hg_old_method(platform, factory, repourl, workdir, revision):
    # baaaaaah.  Seems that the Mercurial class doesn't support
    # updating to a different branch than the one specified by
    # the user (like "default").  This is nonsense if we need
    # an auxiliary check-out :-(  At least I didn't find how.
    if platform in ("win32", "win64"):
        command = "if NOT EXIST .hg rmdir /q /s ."
    else:
        command = "if [ ! -d .hg ]; then rm -fr * .[a-z]*; fi"
    factory.addStep(ShellCmd(description="rmdir?",
                             command=command,
                             workdir=workdir,
                             haltOnFailure=False))
    #
    if platform in ("win32", "win64"):
        command = "if NOT EXIST .hg %s"
    else:
        command = "if [ ! -d .hg ]; then %s; fi"
    command = command % ("hg clone -U " + repourl + " .")
    factory.addStep(ShellCmd(description="hg clone",
                             command=command,
                             workdir=workdir,
                             timeout=3600,
                             haltOnFailure=True))
    #
    factory.addStep(
        ShellCmd(description="hg purge",
                 command="hg --config extensions.purge= purge --all",
                 workdir=workdir,
                 haltOnFailure=True))
    #
    factory.addStep(ShellCmd(description="hg pull",
                             command="hg pull %s" % repourl,
                             workdir=workdir))
    #
    # here, update without caring about branches
    factory.addStep(ShellCmd(description="hg update",
           command="hg update --clean %s" % revision,
           workdir=workdir))

def update_hg(platform, factory, repourl, workdir, revision, use_branch,
              force_branch=None, wipe_bookmarks=False):
    if not use_branch:
        assert force_branch is None
        update_hg_old_method(platform, factory, repourl, workdir, revision)
        return

    if platform in ("win32", "win64"):
        # Clean out files via hackery to avoid long filename limitations in hg
        command = ('hg update -r null & FOR /D %%F in (pypy,lib_pypy,extra_tests) '
                   'DO IF EXIST %%F rmdir /q /s %%F')
        factory.addStep(
            ShellCmd(description="clean up files",
                     command=command,
                     workdir=workdir,
                     haltOnFailure=False))

    if wipe_bookmarks:
        # We don't use bookmarks at all.  If a bookmark accidentally gets
        # created and pushed to the server and we pull it down, it gets stuck
        # here.  Deleting it from the server doesn't seem to delete it from
        # the local checkout.  So, manually clean it up.
        if platform in ('win32', 'win64'):
            command = [r"cmd /c if EXIST .hg\bookmarks del .hg\bookmarks"]
        else:
            command=["rm", "-f", ".hg/bookmarks"]
        factory.addStep(ShellCmd(
            description="cleanup bookmarks",
            command = command,
            workdir=workdir,
            haltOnFailure=False,
        ))

    factory.addStep(
            Mercurial(
                repourl=repourl,
                mode='full',
                method='fresh',
                defaultBranch=force_branch,
                branchType='inrepo',
                clobberOnBranchChange=False,
                workdir=workdir,
                logEnviron=False))



def setup_steps(platform, factory, workdir=None,
                repourl='https://github.com/pypy/pypy/',
                force_branch=None):
    factory.addStep(shell.SetPropertyFromCommand(
            command=['python', '-c', "import tempfile, os; print(tempfile.gettempdir() + os.path.sep)"],
            property="target_tmpdir",
            env={'TMPDIR': "${TMPDIR}"},
    ))
    # If target_tmpdir is empty, crash.
    factory.tmp_dir = '%(prop:target_tmpdir:-crazy/name/so/mkdir/fails/)s'
    factory.pytest = "pytest"
    factory.addStep(ShellCmd(
        description="mkdir for tests",
        command=['python', '-c', Interpolate("import os;  os.mkdir(r'" + \
                    factory.tmp_dir + factory.pytest + "') if not os.path.exists(r'" + \
                    factory.tmp_dir + factory.pytest + "') else True")],
        haltOnFailure=True,
        ))

    factory.addStep(ParseRevision(hideStepIf=ParseRevision.hideStepIf,
                                  doStepIf=ParseRevision.doStepIf))
    #
    revision=WithProperties("%(revision)s")
    # update_hg(platform, factory, repourl, workdir, revision, use_branch=True,
    #          force_branch=force_branch, wipe_bookmarks=True)
    factory.addStep(Git(
            repourl=repourl,
            mode='full',
            method='fresh',
            workdir=workdir,
            branch=force_branch,
            timeout=40*60,
            logEnviron=False))
    #
    factory.addStep(CheckGotRevision(workdir=workdir))

    factory.addStep(ShellCmd(
        description="fetch external dependencies",
        command=['python', 'get_externals.py', '--verbose',
                 '--platform=%s' % platform,],
        flunkOnFailure=False,
        haltOnFailure=False,
        workdir=workdir))

    def extract_info(rc, stdout, stderr):
        if rc == 0:
            return json.loads(stdout)
        else:
            return {}
    factory.addStep(shell.SetPropertyFromCommand(
        command=['python', 'testrunner/get_info.py'],
        extract_fn=extract_info))

def build_name(platform, jit=False, flags=[], placeholder=None):
    if placeholder is None:
        placeholder = '%(final_file_name)s'
    if jit or '-Ojit' in flags:
        kind = 'jit'
    else:
        if '--stackless' in flags:
            kind = 'stackless'
        elif '-Ojit' in flags:
            kind = 'jitnojit'
        elif '-O2' in flags:
            kind = 'nojit'
        else:
            kind = 'unknown'
    return 'pypy-c-' + kind + '-%s-' % (placeholder,) + platform


def get_extension(platform):
    if platform in ("win32", "win64"):
        return ".zip"
    else:
        return ".tar.bz2"

def add_translated_tests(factory, prefix, platform, app_tests, lib_python, pypyjit):
    nDays = '3' #str, not int
    if platform in ("win32", "win64"):
        command = ['FORFILES', '/P', Interpolate(factory.tmp_dir + factory.pytest),
                   '/D', '-' + nDays, '/c', "cmd /c rmdir /q /s @path"]
    else:
        command = ['find', Interpolate(factory.tmp_dir + factory.pytest), '-mtime',
                   '+' + nDays, '-exec', 'rm', '-r', '{}', ';']
    factory.addStep(SuccessAlways(
        description="cleanout old test files",
        command = command,
        flunkOnFailure=False,
        haltOnFailure=False,
        ))

    if lib_python:
        factory.addStep(PytestCmd(
            description="lib-python test",
            command=prefix + ["python", "testrunner/lib_python_tests.py"],
            timeout=4000,
            logfiles={'pytestLog': 'cpython.log'},
            env={"TMPDIR": Interpolate('%(prop:target_tmpdir)s' + factory.pytest),
                 "SETUPTOOLS_USE_DISTUTILS": "stdlib",
                }))

    if app_tests:
        if app_tests is True:
            app_tests = []
        factory.addStep(PytestCmd(
            description="app-level (-A) test",
            command=prefix + ["python", "testrunner/app_level_tests.py",
                     ] + ["--config=%s" % cfg for cfg in app_tests],
            logfiles={'pytestLog': 'pytest-A.log'},
            timeout=4000,
            env={"TMPDIR": Interpolate('%(prop:target_tmpdir)s' + factory.pytest),
                }))
        # set from testrunner/get_info.py
        if platform in ("win32", "win64"):
            virt_pypy = r'pypy-venv\Scripts\python.exe'
            clean = 'if EXIST pypy-venv rmdir /s /q pypy-venv'
        else:
            virt_pypy = 'pypy-venv/bin/python'
            clean = 'rm -rf pypy-venv'
        factory.addStep(ShellCmd(
            description="clean old virtualenv",
            command=clean,
            workdir='venv',
            haltOnFailure=False))
        target = Property('target_path')
        venv_dir = Property('venv_dir', default = 'pypy-venv')
        virt_pypy = Property('virt_pypy', default=virt_pypy)
        xdist_arg = Property('xdist_arg', default='')
        xdist_n = Property('xdist_n', default='')
        # If we already have a bin directory, virtualenv will expect to find
        # the executables there (on linux). So copy them over.
        if platform.startswith('linux') or platform in ('aarch64', 's390x'):
            factory.addStep(ShellCmd(
                    description="copy executable to bin",
                    # Need to use list for Property in command
                    command=['cp', target, 'bin/pypy'],
                ))
            factory.addStep(ShellCmd(
                    description="copy *.so to bin",
                    # Need to use string for '*' in command
                    command='cp pypy/goal/*.so bin',
                ))
        factory.addStep(ShellCmd(
            description="Install recent virtualenv",
            command=prefix + [target, '-mpip', 'install', '--upgrade',
                              '--no-warn-script-location',
                              'pip', 'setuptools', 'virtualenv'],
            workdir='venv',
            flunkOnFailure=True))
        factory.addStep(ShellCmd(
            description="Create virtualenv",
            command=prefix + [target, '-mvirtualenv', '--clear', venv_dir],
            workdir='venv',
            flunkOnFailure=True))
        factory.addStep(ShellCmd(
            description="Install extra tests requirements",
            command=prefix + [virt_pypy, '-m', 'pip', 'install',
                '--no-warn-script-location',
                '-r', '../build/extra_tests/requirements.txt'],
            workdir='venv'))
        factory.addStep(PytestCmd(
            description="Run -D tests",
            command=prefix + [virt_pypy, '-m', 'pytest', '-D',
                '../build/pypy', '--junitxml=test-D.log'],
            logfiles={'pytestLog': 'test-D.log'},
            workdir='venv'))
        factory.addStep(PytestCmd(
            description="Run extra tests",
            command=prefix + [virt_pypy, '-m', 'pytest',
                '../build/extra_tests', '--junitxml=extra.log',
                '--durations=20', '-raw', xdist_arg, xdist_n],
            logfiles={'pytestLog': 'extra.log'},
            workdir='venv',
        ))

    if pypyjit:
        factory.addStep(PytestCmd(
            description="pypyjit tests",
            command=prefix + ["python", "testrunner/pypyjit_tests.py"],
            timeout=4000,
            logfiles={'pytestLog': 'pypyjit_new.log'},
            env={"TMPDIR": Interpolate('%(prop:target_tmpdir)s' + factory.pytest),
                }))


# ----


class Untranslated(factory.BuildFactory):
    def __init__(self, platform='linux', cherrypick='', extra_cfgs=[], **kwargs):
        factory.BuildFactory.__init__(self)

        setup_steps(platform, self)

        self.timeout=kwargs.get('timeout', 4000)

        nDays = '3' #str, not int
        if platform in ("win32", "win64"):
            command = ['FORFILES', '/P', Interpolate(self.tmp_dir + self.pytest),
                       '/D', '-' + nDays, '/c', "cmd /c rmdir /q /s @path"]
        else:
            command = ['find', Interpolate(self.tmp_dir + self.pytest), '-mtime',
                       '+' + nDays, '-exec', 'rm', '-r', '{}', ';']
        self.addStep(SuccessAlways(
            description="cleanout old test files",
            command = command,
            flunkOnFailure=False,
            haltOnFailure=False,
            ))

        if platform in ("win32", "win64"):
            self.virt_python = r'virt_test\Scripts\python.exe'
        else:
            self.virt_python = 'virt_test/bin/python'
        self.addStep(ShellCmd(
            description="create virtualenv for tests",
            command=['virtualenv', 'virt_test'],
            haltOnFailure=True,
            ))

        self.addStep(ShellCmd(
            description="update pip",
            command=[self.virt_python, '-mpip', 'install', '--upgrade',
                     'pip' , 'setuptools'],
            haltOnFailure=True,
            ))

        self.addStep(ShellCmd(
            description="install requirements to virtual environment",
            command=[self.virt_python, '-mpip', 'install', '-r',
                     'requirements.txt'],
            haltOnFailure=True,
            ))



class Own(Untranslated):
    def __init__(self, platform='linux', cherrypick='', extra_cfgs=[], **kwargs):
        Untranslated.__init__(self, platform=platform, cherrypick=cherrypick,
                              extra_cfgs=extra_cfgs, **kwargs)
        self.addStep(PytestCmd(
            description="pytest pypy",
            command=[self.virt_python, "testrunner/runner.py",
                     "--logfile=testrun.log",
                     "--config=pypy/testrunner_cfg.py",
                     "--config=~/machine_cfg.py",
                     "--root=pypy", "--timeout=%s" % (self.timeout,)
                     ] + ["--config=%s" % cfg for cfg in extra_cfgs],
            logfiles={'pytestLog': 'testrun.log'},
            timeout=self.timeout,
            env={"PYTHONPATH": ['.'],
                 "PYPYCHERRYPICK": cherrypick,
                 "TMPDIR": Interpolate(self.tmp_dir + self.pytest),
                 }))

class RPython(Untranslated):
    def __init__(self, platform='linux', cherrypick='', extra_cfgs=[], **kwargs):
        Untranslated.__init__(self, platform=platform, cherrypick=cherrypick,
                              extra_cfgs=extra_cfgs, **kwargs)
        self.addStep(PytestCmd(
            description="pytest rpython",
            command=[self.virt_python, "testrunner/runner.py",
                     "--logfile=testrun.log",
                     "--config=pypy/testrunner_cfg.py",
                     "--config=~/machine_cfg.py",
                     "--root=rpython", "--timeout=%s" % (self.timeout,)
                     ] + ["--config=%s" % cfg for cfg in extra_cfgs],
            logfiles={'pytestLog': 'testrun.log'},
            timeout=self.timeout,
            env={"PYTHONPATH": ['.'],
                 "PYPYCHERRYPICK": cherrypick,
                 "TMPDIR": Interpolate(self.tmp_dir + self.pytest),
                 }))


class Translated(factory.BuildFactory):

    def __init__(self, platform='linux',
                 translationArgs=['-O2'], targetArgs=[],
                 app_tests=False,
                 interpreter='pypy',
                 lib_python=False,
                 pypyjit=False,
                 prefix=None,
                 trigger=None,
                 ):
        factory.BuildFactory.__init__(self)
        if prefix is not None:
            prefix = prefix.split()
        else:
            prefix = []

        setup_steps(platform, self)

        self.addStep(Translate(translationArgs, targetArgs,
                               interpreter=interpreter))

        # win32 needs setuptools to successfully build the cffi extensions
        target = Property('target_path')
        self.addStep(ShellCmd(
            description="ensurepip",
            command=prefix + [target, '-mensurepip'],
            flunkOnFailure=True))

        name = build_name(platform, pypyjit, translationArgs)
        self.addStep(ShellCmd(
            description="compress pypy-c",
            haltOnFailure=False,
            command=prefix + ["python", "pypy/tool/release/package.py",
                              "--targetdir=.",
                              "--archive-name", WithProperties(name)],
            workdir='build',
            env={
                 "TMPDIR": Interpolate(self.tmp_dir + self.pytest),
                },
            ))
        nightly = '~/nightly/'
        extension = '%(extension:~' + get_extension(platform) + ')s'
        pypy_c_rel = "build/" + name + extension
        self.addStep(PyPyUpload(slavesrc=WithProperties(pypy_c_rel),
                                masterdest=WithProperties(nightly),
                                basename=name + extension,
                                workdir='.',
                                blocksize=100 * 1024))

        if trigger: # if provided trigger schedulers that depend on this one
            self.addStep(Trigger(schedulerNames=[trigger]))

        add_translated_tests(self, prefix, platform, app_tests, lib_python, pypyjit)


class TranslatedTests(factory.BuildFactory):
    '''
    Download a pypy nightly build and run the app-level tests on the binary
    '''

    def __init__(self, platform='linux',
                 app_tests=False,
                 lib_python=False,
                 pypyjit=False,
                 prefix=None,
                 translationArgs=[]
                 ):
        factory.BuildFactory.__init__(self)
        if prefix is not None:
            prefix = prefix.split()
        else:
            prefix = []

        # XXX extend to checkout the specific revision of the build
        setup_steps(platform, self)

        # download corresponding nightly build
        self.addStep(ShellCmd(
            description="Clear pypy-c",
            command=['rm', '-rf', 'pypy-c'],
            workdir='.'))
        extension = '%(extension:~' + get_extension(platform) + ')s'
        name = build_name(platform, pypyjit, translationArgs, placeholder='%(final_file_name)s') + extension
        self.addStep(PyPyDownload(
            basename=name,
            mastersrc='~/nightly',
            slavedest=WithProperties('pypy_build' + extension),
            workdir='pypy-c'))

        # extract downloaded file
        if platform.startswith('win'):
            raise NotImplementedError
        else:
            self.addStep(ShellCmd(
                description="decompress pypy-c",
                command=['tar', '--extract', WithProperties('--file=pypy_build' + extension), '--strip-components=1', '--directory=.'],
                workdir='pypy-c',
                haltOnFailure=True,
                ))

        self.addStep(ShellCmd(
            description="reset permissions",
            command=['chmod', 'u+rw', '-R', 'build/include'],
            haltOnFailure=True,
            workdir='.'))
        # copy pypy-c to the expected location within the pypy source checkout
        command = ('PYPY_C="pypy";'
                   'if [ -e pypy-c/bin/pypy3 ]; then PYPY_C="pypy3"; fi;'
                   'cp -v pypy-c/bin/$PYPY_C build/pypy/goal/$PYPY_C-c;')
        self.addStep(ShellCmd(
            description="copy pypy-c",
            command=command,
            haltOnFailure=True,
            workdir='.'))
        # copy libpypy-c.so to the expected location within the pypy source checkout, if available
        command = 'cp -v pypy-c/bin/libpypy*-c.so build/pypy/goal/ || true'
        self.addStep(ShellCmd(
            description="copy libpypy-c.so",
            command=command,
            haltOnFailure=True,
            workdir='.'))
        # copy generated and copied header files to build/include
        self.addStep(ShellCmd(
            description="copy header files",
            command=['cp', '-vr', 'pypy-c/include', 'build'],
            haltOnFailure=True,
            workdir='.'))
        # copy ctypes_resource_cache generated during translation
        self.addStep(ShellCmd(
            description="reset permissions",
            command=['chmod', 'u+rw', '-R', 'build/lib_pypy'],
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description="copy cffi import libraries",
            command='cp -rv pypy-c/lib_pypy/*.so build/lib_pypy',
            haltOnFailure=True,
            workdir='.'))

        add_translated_tests(self, prefix, platform, app_tests, lib_python, pypyjit)


class NightlyBuild(factory.BuildFactory):
    def __init__(self, platform='linux',
                 translationArgs=['-O2'], targetArgs=[],
                 interpreter='pypy',
                 prefix=[],
                 trigger=None,
                 ):
        factory.BuildFactory.__init__(self)

        setup_steps(platform, self)

        self.addStep(Translate(translationArgs, targetArgs,
                               interpreter=interpreter))

        name = build_name(platform, flags=translationArgs)
        self.addStep(ShellCmd(
            description="compress pypy-c",
            command=prefix + ["python", "pypy/tool/release/package.py",
                              "--targetdir=.",
                              "--archive-name", WithProperties(name)],
            haltOnFailure=True,
            workdir='build'))
        nightly = '~/nightly/'
        extension = '%(extension:~' + get_extension(platform) + ')s'
        pypy_c_rel = "build/" + name + extension
        self.addStep(PyPyUpload(slavesrc=WithProperties(pypy_c_rel),
                                masterdest=WithProperties(nightly),
                                basename=name + extension,
                                workdir='.',
                                blocksize=100 * 1024))
        if trigger: # if provided trigger schedulers that depend on this one
            self.addStep(Trigger(schedulerNames=[trigger]))

class JITBenchmarkSingleRun(factory.BuildFactory):
    def __init__(self, platform='linux', host='speed_python', postfix=''):
        factory.BuildFactory.__init__(self)

        # Always use the latest version on the single-run branch of the
        # benchmark repo,
        # branch and revision refer to the pypy version to benchmark
        repourl = 'https://foss.heptapod.net/pypy/benchmarks'
        update_hg(platform, self, repourl, 'benchmarks', '', use_branch=True,
                  force_branch='single-run')
        #
        setup_steps(platform, self)
        if host == 'benchmarker':
            lock = BenchmarkerLock
        elif host == 'benchmarker2':
            lock = Benchmarker2Lock
        elif host == 'speed_python':
            lock = SpeedPythonCPU
        else:
            assert False, 'unknown host %s' % host

        self.addStep(
            Translate(
                translationArgs=['-Ojit'],
                targetArgs=[],
                haltOnFailure=True,
                # this step can be executed in parallel with other builds
                locks=[lock.access('counting')],
                )
            )
        pypy_c_rel = "../build/pypy/goal/pypy-c"
        self.addStep(ShellCmd(
            # this step needs exclusive access to the CPU
            locks=[lock.access('exclusive')],
            description="run benchmarks on top of pypy-c",
            command=["python", "runner.py", '--output-filename', 'result.json',
                     '--python', pypy_c_rel,
                     '--revision', WithProperties('%(got_revision)s'),
                     '--branch', WithProperties('%(branch)s'),
                     '--force-interpreter-name', 'pypy-c-jit',
                     ],
            workdir='./benchmarks',
            timeout=3600))
        # a bit obscure hack to get both os.path.expand and a property
        filename = '%(got_revision)s' + (postfix or '')
        resfile = os.path.expanduser("~/bench_results_new/%s.json" % filename)
        self.addStep(transfer.FileUpload(slavesrc="benchmarks/result.json",
                                         masterdest=WithProperties(resfile),
                                         workdir="."))

class JITBenchmark(factory.BuildFactory):
    def __init__(self, platform='linux', host='benchmarker', postfix='',
                 upload_credentials=None):
        factory.BuildFactory.__init__(self)

        #
        repourl = 'https://foss.heptapod.net/pypy/benchmarks'
        # benchmark_branch is the branch in the benchmark repo,
        # the rest refer to the pypy version to benchmark
       
        # Since we want to use the benchmark_branch, copy the hg update steps
        if platform in ("win32", "win64"):
            command = "if NOT EXIST .hg rmdir /q /s ."
        else:
            command = "if [ ! -d .hg ]; then rm -fr * .[a-z]*; fi"
        self.addStep(ShellCmd(description="rmdir?",
                                 command=command,
                                 workdir='./benchmarks',
                                 haltOnFailure=False))
        if platform in ("win32", "win64"):
            command = "if NOT EXIST .hg %s"
        else:
            command = "if [ ! -d .hg ]; then %s; fi"
        command = command % ("hg clone -U " + repourl + " .")
        self.addStep(ShellCmd(description="hg clone",
                                 command=command,
                                 workdir='./benchmarks',
                                 timeout=3600,
                                 haltOnFailure=True))
        self.addStep(
            ShellCmd(description="benchmrk: hg purge",
                 command="hg --config extensions.purge= purge --all",
                 workdir='./benchmarks',
                 haltOnFailure=True))
        self.addStep(ShellCmd(description="benchmrk: hg pull",
                                 command="hg pull %s" % repourl,
                                 workdir='./benchmarks'))
        self.addStep(ShellCmd(description="benchmrk: hg update",
            command=Interpolate("hg update --clean %(prop:benchmark_branch)s"),
            workdir='./benchmarks'))
        self.addStep(ShellCmd(description="benchmrk: hg report revision",
            command=Interpolate("hg parents --template='got_revision:{rev}:{node}'"),
            workdir='./benchmarks'))

        setup_steps(platform, self)
        if host == 'benchmarker':
            lock = BenchmarkerLock
        elif host == 'benchmarker2':
            lock = Benchmarker2Lock
        elif host == 'speed_python':
            lock = SpeedPythonCPU
        else:
            assert False, 'unknown host %s' % host

        upload_env = {
            'SPEED_UPLOAD_URL': os.environ.get('SPEED_UPLOAD_URL', 'https://speed.pypy.org/'),
            'SPEED_UPLOAD_HOST': os.environ.get('SPEED_UPLOAD_HOST', host),
        }
        if upload_credentials:
            upload_env['SPEED_UPLOAD_USER'] = upload_credentials.get('username', '')
            upload_env['SPEED_UPLOAD_PASSWORD'] = upload_credentials.get('password', '')

        def extract_upload_config(rc, stdout, stderr):
            return {'upload_url': upload_env['SPEED_UPLOAD_URL'],
                    'upload_host': upload_env['SPEED_UPLOAD_HOST']}
        self.addStep(shell.SetPropertyFromCommand(
            command=['python', '-c', 'print("ok")'],
            extract_fn=extract_upload_config,
            description='set upload config',
        ))

        self.addStep(
            Translate(
                translationArgs=['-Ojit'],
                targetArgs=[],
                haltOnFailure=True,
                # this step can be executed in parallel with other builds
                locks=[lock.access('counting')],
                )
            )

        @renderer
        def get_cmd(props):
            # set from testrunner/get_info.py
            target = props.getProperty('target_path')
            exe = os.path.split(target)[-1][:-2]
            project = props.getProperty('project', default='PyPy')
            rev = props.getProperty('got_revision')
            rev = rev.split(':')[-1]
            branch = props.getProperty('branch')
            if branch == 'None' or branch is None:
                branch = 'main'
            command=["python", "-u", "runner.py", '--output-filename', 'result.json',
                     '--changed', target,
                     '--baseline', target,
                     '--args', ',--jit off',
                     '--revision', rev,
                     '--branch', branch,
                     ]
            return command

        # Push bulk_upload.py to the slave so upload steps can use it
        self.addStep(transfer.FileDownload(
            mastersrc=os.path.join(os.path.dirname(__file__), 'bulk_upload.py'),
            slavedest='bulk_upload.py',
            workdir='.'))

        def _props_for_upload(props):
            target = props.getProperty('target_path')
            exe = os.path.split(target)[-1][:-2]
            project = props.getProperty('project', default='PyPy')
            rev = props.getProperty('got_revision').split(':')[-1]
            branch = props.getProperty('branch') or 'main'
            if branch == 'None':
                branch = 'main'
            return exe, project, rev, branch

        @renderer
        def get_upload_changed_cmd(props):
            exe, project, rev, branch = _props_for_upload(props)
            return ['python3', 'bulk_upload.py', 'benchmarks/result.json',
                    '-e', exe + postfix, '-H', upload_env['SPEED_UPLOAD_HOST'],
                    '-P', project, '-r', rev, '-B', branch,
                    '-u', upload_env['SPEED_UPLOAD_URL']]

        @renderer
        def get_upload_baseline_cmd(props):
            exe, project, rev, branch = _props_for_upload(props)
            return ['python3', 'bulk_upload.py', 'benchmarks/result.json',
                    '-e', exe + '-jit' + postfix, '-H', upload_env['SPEED_UPLOAD_HOST'],
                    '-P', project, '-r', rev, '-B', branch,
                    '-u', upload_env['SPEED_UPLOAD_URL'],
                    '--baseline']

        # Pyperformance: only when the target binary is pypy3*
        def is_py3_target(step):
            target = step.build.getProperty('target_path') or ''
            return os.path.basename(target).startswith('pypy3')

        @renderer
        def get_pyperformance_venv_cmd(props):
            target = props.getProperty('target_path')
            return [target, '-m', 'venv', 'pyperformance_venv']

        @renderer
        def get_pyperformance_install_cmd(props):
            spec = props.getProperty('pyperformance_spec', default='pyperformance').strip()
            return ['./pyperformance_venv/bin/pip', 'install', '--upgrade', spec]

        @renderer
        def get_pyperformance_bench_venv_cmd(props):
            target = props.getProperty('target_path')
            return ['./pyperformance_venv/bin/python', '-m', 'pyperformance',
                    'venv', 'recreate', '-p', target]

        def get_pyperformance_run_cmd(outfile, inherit_environ=None, fast=False):
            @renderer
            def _cmd(props):
                target = props.getProperty('target_path')
                inherit = ('--inherit-environ %s ' % inherit_environ
                           if inherit_environ else '')
                fast_flag = '-f ' if fast else ''
                return ['bash', '-c',
                        'rm -f %s && '
                        './pyperformance_venv/bin/python -m pyperformance run '
                        '%s%s--python %s --output %s' % (outfile, fast_flag, inherit, target, outfile)]
            return _cmd

        @renderer
        def get_pyperformance_upload_cmd(props):
            exe, project, rev, branch = _props_for_upload(props)
            return ['python3', 'bulk_upload.py', 'pyperformance_result.json',
                    '-e', exe + '-jit' + postfix, '-H', upload_env['SPEED_UPLOAD_HOST'],
                    '-P', project, '-r', rev, '-B', branch,
                    '-u', upload_env['SPEED_UPLOAD_URL']]

        @renderer
        def get_pyperformance_nojit_upload_cmd(props):
            exe, project, rev, branch = _props_for_upload(props)
            return ['python3', 'bulk_upload.py', 'pyperformance_nojit_result.json',
                    '-e', exe + postfix, '-H', upload_env['SPEED_UPLOAD_HOST'],
                    '-P', project, '-r', rev, '-B', branch,
                    '-u', upload_env['SPEED_UPLOAD_URL']]

        self.addStep(ShellCmd(
            description='clean up old pypy venvs',
            command=['sh', '-c', 'rm -rf venv pyperformance_venv'],
            doStepIf=is_py3_target,
            workdir='.'))
        self.addStep(ShellCmd(
            description='create pyperformance venv',
            command=get_pyperformance_venv_cmd,
            doStepIf=is_py3_target,
            workdir='.'))
        self.addStep(ShellCmd(
            description='install pyperformance',
            command=get_pyperformance_install_cmd,
            doStepIf=is_py3_target,
            workdir='.'))
        self.addStep(ShellCmd(
            description='create pyperformance benchmark venv',
            command=get_pyperformance_bench_venv_cmd,
            doStepIf=is_py3_target,
            workdir='.'))

        # Transfer all PyPy-compatibility patch scripts from master to worker,
        # then apply them in a single step.
        _patches_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..', 'patches'))
        for _patch_file in sorted(glob.glob(
                os.path.join(_patches_dir, 'patch_*_pypy.py'))):
            self.addStep(transfer.FileDownload(
                mastersrc=_patch_file,
                slavedest=os.path.basename(_patch_file),
                doStepIf=is_py3_target,
                workdir='.'))
        self.addStep(ShellCmd(
            description='apply PyPy compatibility patches',
            command=['python3', '-c',
                     'import glob, os, subprocess, sys\n'
                     'print("cwd:", os.getcwd())\n'
                     'print("cwd contents:", sorted(os.listdir(".")))\n'
                     'venv_dir = "venv"\n'
                     'if os.path.isdir(venv_dir):\n'
                     '    print("venv/ contents:", sorted(os.listdir(venv_dir)))\n'
                     'else:\n'
                     '    print("venv/ directory does not exist")\n'
                     'scripts = sorted(glob.glob("patch_*_pypy.py"))\n'
                     'print("Applying patches:", scripts)\n'
                     'for f in scripts:\n'
                     '    subprocess.check_call([sys.executable, f])\n'],
            doStepIf=is_py3_target,
            haltOnFailure=True,
            workdir='.'))

        self.addStep(ShellCmd(
            description='run pyperformance (jit)',
            command=get_pyperformance_run_cmd('pyperformance_result.json'),
            locks=[lock.access('exclusive')],
            doStepIf=is_py3_target,
            workdir='.',
            timeout=7200))
        self.addStep(ShellCmd(
            description='upload pyperformance results (jit)',
            command=get_pyperformance_upload_cmd,
            env=upload_env,
            doStepIf=is_py3_target,
            workdir='.'))
        self.addStep(ShellCmd(
            description='run pyperformance (nojit)',
            command=get_pyperformance_run_cmd('pyperformance_nojit_result.json',
                                              inherit_environ='PYPY_DISABLE_JIT',
                                              fast=True),
            env={'PYPY_DISABLE_JIT': '1'},
            locks=[lock.access('exclusive')],
            doStepIf=is_py3_target,
            workdir='.',
            timeout=7200))
        self.addStep(ShellCmd(
            description='upload pyperformance results (nojit)',
            command=get_pyperformance_nojit_upload_cmd,
            env=upload_env,
            doStepIf=is_py3_target,
            workdir='.'))

        self.addStep(ShellCmd(
            # this step needs exclusive access to the CPU
            locks=[lock.access('exclusive')],
            description="run benchmarks on top of pypy-c",
            command=get_cmd,
            workdir='./benchmarks',
            timeout=3600))
        self.addStep(ShellCmd(
            description='upload legacy results (jit-off)',
            command=get_upload_changed_cmd,
            env=upload_env,
            workdir='.'))
        self.addStep(ShellCmd(
            description='upload legacy results (jit-on)',
            command=get_upload_baseline_cmd,
            env=upload_env,
            workdir='.'))

        # Archive the legacy result file on the master
        filename = '%(got_revision)s' + (postfix or '')
        resfile = os.path.expanduser("~/bench_results/%s-%s.json" % (filename, host))
        self.addStep(transfer.FileUpload(slavesrc="benchmarks/result.json",
                                         masterdest=WithProperties(resfile),
                                         workdir="."))
        pyresfile = os.path.expanduser("~/bench_results/%s-pyperformance.json" % filename)
        self.addStep(transfer.FileUpload(slavesrc="pyperformance_result.json",
                                         masterdest=WithProperties(pyresfile),
                                         doStepIf=is_py3_target,
                                         workdir="."))
        pynoresfile = os.path.expanduser("~/bench_results/%s-pyperformance-nojit.json" % filename)
        self.addStep(transfer.FileUpload(slavesrc="pyperformance_nojit_result.json",
                                         masterdest=WithProperties(pynoresfile),
                                         doStepIf=is_py3_target,
                                         workdir="."))


class CPythonBenchmark(factory.BuildFactory):
    '''
    Check out and build CPython and run the benchmarks with it.

    This will overwrite the branch even if it was specified
    in the buildbot webinterface!
    '''
    def __init__(self, branch, platform='linux64'):
        '''
        branch: The branch of cpython that will be used.
        '''
        factory.BuildFactory.__init__(self)

        # check out and update benchmarks
        repourl = 'https://foss.heptapod.net/pypy/benchmarks'
        update_hg(platform, self, repourl, 'benchmarks', 'default', use_branch=False)

        # checks out and updates the repo
        setup_steps(platform, self, repourl='http://hg.python.org/cpython',
                    force_branch=branch)

        lock = SpeedPythonCPU

        self.addStep(ShellCmd(
            description="configure cpython",
            command=["./configure"],
            timeout=300,
            haltOnFailure=True))

        self.addStep(ShellCmd(
            description="cleanup cpython",
            command=["make", "clean"],
            timeout=300))

        self.addStep(ShellCmd(
            description="make cpython",
            command=["make"],
            timeout=600,
            haltOnFailure=True))

        self.addStep(ShellCmd(
            description="test cpython",
            command=["make", "buildbottest"],
            haltOnFailure=False,
            warnOnFailure=True,
            timeout=600))

        cpython_interpreter = '../build/python'
        self.addStep(ShellCmd(
            # this step needs exclusive access to the CPU
            locks=[lock.access('exclusive')],
            description="run benchmarks on top of cpython",
            command=["python", "runner.py", '--output-filename', 'result.json',
                     '--changed', cpython_interpreter,
                     '--baseline', './nullpython.py',
                     '--upload',
                     '--upload-project', 'cpython',
                     '--upload-executable', 'cpython2',
                     '--revision', WithProperties('%(got_revision)s'),
                     '--branch', WithProperties('%(branch)s'),
                     '--upload-urls', 'http://localhost/',
                     ],
            workdir='./benchmarks',
            haltOnFailure=True,
            timeout=3600))

        # a bit obscure hack to get both os.path.expand and a property
        filename = '%(got_revision)s'
        resultfile = os.path.expanduser("~/bench_results/%s.json" % filename)
        self.addStep(transfer.FileUpload(slavesrc="benchmarks/result.json",
                                         masterdest=WithProperties(resultfile),
                                         workdir="."))

class PyPyBuildbotTestFactory(factory.BuildFactory):
    def __init__(self):
        factory.BuildFactory.__init__(self)
        # clone
        self.addStep(
            Mercurial(
                repourl='https://foss.heptapod.net/pypy/buildbot',
                mode='incremental',
                method='fresh',
                defaultBranch='default',
                branchType='inrepo',
                clobberOnBranchChange=False,
                logEnviron=False))
        # create a virtualenv
        self.addStep(ShellCmd(
            description='create virtualenv',
            haltOnFailure=True,
            command='virtualenv --clear ../venv'))
        # install deps
        self.addStep(ShellCmd(
            description="install dependencies",
            haltOnFailure=True,
            command=('../venv/bin/pip install -r requirements.txt').split()))
        # run tests
        self.addStep(PytestCmd(
            description="pytest buildbot",
            haltOnFailure=True,
            command=["../venv/bin/py.test",
                     "--resultlog=testrun.log",
                     ],
            logfiles={'pytestLog': 'testrun.log'}))


class NativeNumpyTests(factory.BuildFactory):
    '''
    Download a pypy nightly, install nose and numpy, and run the numpy test suite
    '''
    def __init__(self, platform='linux',
                 app_tests=False,
                 lib_python=False,
                 pypyjit=True,
                 prefix=None,
                 translationArgs=[]
                 ):
        factory.BuildFactory.__init__(self)

        self.addStep(ParseRevision(hideStepIf=ParseRevision.hideStepIf,
                                  doStepIf=ParseRevision.doStepIf))
        # download corresponding nightly build
        if platform in ("win32", "win64"):
            target = r'pypy-c\pypy.exe'
            untar = ['unzip']
            sep = '\\'
        else:
            target = r'pypy-c/bin/pypy'
            untar = ['tar', '--strip-components=1', '--directory=.', '-xf']
            sep = '/'
        self.addStep(ShellCmd(
            description="Clear",
            # assume, as part of git, that windows has rm
            command=['rm', '-rf', 'pypy-c', 'install'],
            workdir='.'))
        extension = get_extension(platform)
        name = build_name(platform, pypyjit, translationArgs, placeholder='%(final_file_name)s') + extension
        self.addStep(PyPyDownload(
            basename=name,
            mastersrc='~/nightly',
            slavedest='pypy_build' + extension,
            workdir='pypy-c'))

        # extract downloaded file
        self.addStep(ShellCmd(
            description="decompress pypy-c",
            command=untar + ['pypy_build'+ extension],
            workdir='pypy-c',
            haltOnFailure=True,
            ))

        if platform in ("win32", "win64"):
            self.addStep(ShellCmd(
                description='move decompressed dir',
                command = ['mv', '*/*', '.'],
                workdir='pypy-c',
                haltOnFailure=True,
                ))

        # virtualenv the download
        self.addStep(ShellCmd(
            description="create virtualenv",
            command=['virtualenv','-p', target, 'install'],
            workdir='./',
            haltOnFailure=True,
            ))

        self.addStep(ShellCmd(
            description="report version",
            command=[sep.join(['install','bin','pypy'])] + ['--version'],
            workdir='./',
            haltOnFailure=True,
            ))

        self.addStep(ShellCmd(
            description="install nose",
            command=[sep.join(['install','bin','pip'])] + ['install','nose'],
            workdir='./',
            haltOnFailure=True,
            ))

        # obtain a pypy-compatible branch of numpy
        numpy_url = 'https://foss.heptapod.net/pypy/numpy'
        self.addStep(Git(
            repourl=numpy_url,
            mode='full',
            method='fresh',
            workdir='numpy_src',
            branch='master',
            alwaysUseLatest=True,
            timeout=40*60,
            logEnviron=False))

        self.addStep(ShellCmd(
            description="install numpy",
            command=[sep.join(['..', 'install', 'bin', 'pypy'])] + ['setup.py','install'],
            workdir='numpy_src'))

        self.addStep(ShellCmd(
            description="test numpy",
            command=[sep.join(['..', 'install', 'bin', 'pypy'])] + ['runtests.py'],
            #logfiles={'pytestLog': 'pytest-numpy.log'},
            timeout=4000,
            workdir='numpy_src',
        ))
        if platform in ("win32", "win64"):
            self.addStep(ShellCmd(
                description="install jinja2",
                command=['install/bin/pip', 'install', 'jinja2'],
                workdir='./',
                haltOnFailure=True,))
            pypy_c_rel = 'install/bin/python'
            self.addStep(ShellCmd(
                description="measure numpy compatibility",
                command=[pypy_c_rel,
                         'numpy_src/tools/numready/',
                         pypy_c_rel, 'numpy-compat.html'],
                workdir="."))
            resfile = os.path.expanduser("~/numpy_compat/%(got_revision)s.html")
            self.addStep(NumpyStatusUpload(
                slavesrc="numpy-compat.html",
                masterdest=WithProperties(resfile),
                workdir="."))
