from buildbot.steps.source.mercurial import Mercurial
from buildbot.steps.source.git import Git
from buildbot.process.buildstep import BuildStep
from buildbot.process import factory
from buildbot.steps import shell, transfer
from buildbot.steps.master import SetProperty as MasterSetProperty
from buildbot.steps.trigger import Trigger
from buildbot.process.properties import WithProperties, Interpolate, Property, renderer
from buildbot import locks
from pypybuildbot.util import symlink_force
from buildbot.status.results import SKIPPED, SUCCESS
import base64
import glob
import os
import json
import re

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
WinSlaveLock = locks.SlaveLock('win_cpu', maxCount=1)
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


class UploadCmd(ShellCmd):
    # Upload steps carry SPEED_UPLOAD_PASSWORD in their environment, so they
    # pass logEnviron=False to keep the env out of the log.  That also hides
    # the command, and the waterfall only shows the static description, so
    # echo the rendered command into stdio as a header.
    def startCommand(self, cmd, errorMessages=None):
        command = cmd.command
        if not isinstance(command, basestring):
            command = ' '.join(str(c) for c in command)
        errorMessages = list(errorMessages or []) + ['run command: %s\n' % command]
        return ShellCmd.startCommand(self, cmd, errorMessages)


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
            os.chmod(masterdest, 0775)
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
    def commandComplete(self, cmd):
        from pypybuildbot.summary import RevisionOutcomeSet
        if 'pytestLog' not in cmd.logs:
            return
        pytestLog = cmd.logs['pytestLog']
        outcome = RevisionOutcomeSet(None)
        # Parse the log the same way the summary page does (see
        # summary._load_outcome_set), so this cached summary can never disagree
        # with it: junitxml results go through populate_xml, the old resultlog
        # format through populate.
        if pytestLog.getText().startswith("<?xml "):
            outcome.populate_xml(pytestLog)
        else:
            outcome.populate(pytestLog)
        summary = outcome.get_summary()
        build_status = self.build.build_status
        builder = build_status.builder
        properties = build_status.getProperties()
        if not hasattr(builder, 'summary_by_branch_and_revision'):
            builder.summary_by_branch_and_revision = {}
        try:
            rev = properties['got_revision']
        except KeyError:
            # without a revision there is nothing to key the result on
            return
        # A build may be triggered without a 'branch' property (nightly/forced
        # scheduler).  Don't drop the result in that case: a missing branch is
        # the default branch, which map_branch_name maps to 'main' -- the same
        # branch the nightly download page looks results up by.
        if properties.has_key('branch'):
            branch = map_branch_name(properties['branch'])
        else:
            branch = map_branch_name(None)
        if branch.endswith('/'):
            branch = branch[:-1]
        d = builder.summary_by_branch_and_revision
        key = (branch, rev)
        if key in d:
            summary += d[key]
        d[key] = summary
        builder.saveYourself()

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
    mkdir_pyscript = ("import os;  os.mkdir(r'" + \
                factory.tmp_dir + factory.pytest + "') if not os.path.exists(r'" + \
                factory.tmp_dir + factory.pytest + "') else True")
    if platform in ('win32', 'win64'):
        # Stray pypy*.exe processes left running from a previous test run
        # keep .exe/.dll files locked, which makes the later
        # "git clean -f -d -x" step fail with "Invalid argument".  Kill
        # them here.  taskkill exits non-zero when nothing matches (the
        # common case), so force success with "exit /b 0".
        mkdir_command = Interpolate(
            'taskkill /F /IM pypy3* /T & exit /b 0 & python -c "' +
            mkdir_pyscript + '"')
        mkdir_description = "mkdir pytest and kill stray pypy.exe"
    else:
        mkdir_command = ['python', '-c', Interpolate(mkdir_pyscript)]
        mkdir_description = "mkdir for tests"
    factory.addStep(ShellCmd(
        description=mkdir_description,
        command=mkdir_command,
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
    nDays = '1' #str, not int
    # translation itself does not set TMPDIR, so its usession-* leftovers
    # land directly in the system tmp dir instead of under tmp_dir + pytest
    if platform in ("win32", "win64"):
        command = ['cmd', '/c',
            Interpolate(
                "FORFILES /P " + factory.tmp_dir + factory.pytest +
                " /D -" + nDays + " /c \"cmd /c rmdir /q /s @path\" & " +
                "FORFILES /P " + factory.tmp_dir +
                " /M usession* /D -" + nDays +
                " /c \"cmd /c rmdir /q /s @path\"")]
    else:
        command = ['sh', '-c',
            Interpolate(
                "du -sh /tmp/*; " +
                "find " + factory.tmp_dir + factory.pytest +
                " -mtime +" + nDays + " -print -delete ; " +
                "find " + factory.tmp_dir + "usession* " +
                "-mtime +" + nDays + " -print -delete ; " +
                "df -h /tmp")]
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
        if platform.startswith('linux') or platform.startswith('macos') or platform in ('aarch64', 's390x'):
            factory.addStep(ShellCmd(
                    description="copy executable to bin",
                    # Need to use list for Property in command
                    command=['cp', target, 'bin/pypy'],
                ))
            factory.addStep(ShellCmd(
                    description="copy libpypy* to bin",
                    # Need to use string for '*' in command
                    command='cp pypy/goal/libpypy* bin',
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
        # pip's http cache entry is two files and the body write is silently
        # dropped on failure, which leaves the entry inconsistent and wedges
        # every later build.  Drop those entries before installing, and report
        # the failing writes instead of guessing (issue 5713)
        instrument_pip = platform in ("win32", "win64")
        if instrument_pip:
            factory.addStep(transfer.FileDownload(
                mastersrc=os.path.join(os.path.dirname(__file__),
                                       'pip_cache_debug.py'),
                slavedest='pip_cache_debug.py',
                workdir='venv'))
            factory.addStep(ShellCmd(
                description="instrument pip cache",
                command=prefix + [virt_pypy, 'pip_cache_debug.py',
                                  'instrument'],
                workdir='venv',
                haltOnFailure=False,
                flunkOnFailure=False))
            factory.addStep(ShellCmd(
                description="repair pip cache",
                command=prefix + [virt_pypy, 'pip_cache_debug.py', 'repair'],
                workdir='venv',
                haltOnFailure=False,
                flunkOnFailure=False))
        factory.addStep(ShellCmd(
            description="Install extra tests requirements",
            command=prefix + [virt_pypy, '-m', 'pip', 'install',
                '--no-warn-script-location',
                '-r', '../build/extra_tests/requirements.txt'],
            workdir='venv'))
        if instrument_pip:
            factory.addStep(ShellCmd(
                description="report pip cache",
                command=prefix + [virt_pypy, 'pip_cache_debug.py', 'report'],
                workdir='venv',
                haltOnFailure=False,
                flunkOnFailure=False))
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

def update_benchmarks_repo(platform, factory,
                           repourl='https://foss.heptapod.net/pypy/benchmarks'):
    """Check out the legacy benchmark repo at the 'benchmark_branch' property.

    The stock hg update steps take the branch from the source stamp, which here
    refers to the interpreter being benchmarked, not to the benchmark repo --
    hence the hand-rolled clone/pull/update.
    """
    if platform in ("win32", "win64"):
        command = "if NOT EXIST .hg rmdir /q /s ."
    else:
        command = "if [ ! -d .hg ]; then rm -fr * .[a-z]*; fi"
    factory.addStep(ShellCmd(description="rmdir?",
                             command=command,
                             workdir='./benchmarks',
                             haltOnFailure=False))
    if platform in ("win32", "win64"):
        command = "if NOT EXIST .hg %s"
    else:
        command = "if [ ! -d .hg ]; then %s; fi"
    command = command % ("hg clone -U " + repourl + " .")
    factory.addStep(ShellCmd(description="hg clone",
                             command=command,
                             workdir='./benchmarks',
                             timeout=3600,
                             haltOnFailure=True))
    factory.addStep(
        ShellCmd(description="benchmrk: hg purge",
                 command="hg --config extensions.purge= purge --all",
                 workdir='./benchmarks',
                 haltOnFailure=True))
    factory.addStep(ShellCmd(description="benchmrk: hg pull",
                             command="hg pull %s" % repourl,
                             workdir='./benchmarks'))
    factory.addStep(ShellCmd(description="benchmrk: hg update",
        command=Interpolate("hg update --clean %(prop:benchmark_branch)s"),
        workdir='./benchmarks'))
    factory.addStep(ShellCmd(description="benchmrk: hg report revision",
        command=Interpolate("hg parents --template='got_revision:{rev}:{node}'"),
        workdir='./benchmarks'))


class JITBenchmark(factory.BuildFactory):
    def __init__(self, platform='linux', host='benchmarker', postfix='',
                 upload_credentials=None):
        factory.BuildFactory.__init__(self)

        # benchmark_branch is the branch in the benchmark repo,
        # the rest refer to the pypy version to benchmark
        update_benchmarks_repo(platform, self)

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

        # pydantic-core, pulled in by the pyperformance fastapi benchmark, pins
        # pyo3 ^0.29 and publishes no pp312 wheel.  pyo3 only gained the
        # not(PyPy) gates needed to compile for PyPy 3.12 in 0.30, so build the
        # wheel here with the pyo3 pin bumped and offer it to the benchmark venv
        # through PIP_FIND_LINKS.
        wheelhouse = './wheelhouse'
        pydantic_core_version = '2.50.1'   # pinned by pydantic 2.14.x
        pyo3_version = '0.30'

        build_pydantic_core = """
            set -e
            mkdir -p %(wheelhouse)s
            if ls %(wheelhouse)s/pydantic_core-%(version)s-pp312-*.whl \
                    >/dev/null 2>&1; then
                exit 0
            fi
            tmp=$(mktemp -d)
            trap "rm -rf $tmp" EXIT
            %(target)s -m venv $tmp/venv
            $tmp/venv/bin/pip install --no-cache-dir maturin
            $tmp/venv/bin/pip download --no-deps --no-binary :all: \
                --dest $tmp pydantic-core==%(version)s
            tar xzf $tmp/pydantic_core-%(version)s.tar.gz -C $tmp
            src=$tmp/pydantic_core-%(version)s
            sed -i -E 's/^(pyo3(-build-config)? = \\{ version = )"[^"]+"/\\1"%(pyo3)s"/' \
                $src/Cargo.toml
            rm -f $src/Cargo.lock
            $tmp/venv/bin/maturin build --release --compatibility off \
                -m $src/Cargo.toml -i $tmp/venv/bin/python -o %(wheelhouse)s
        """

        @renderer
        def get_pyperformance_venv_cmd(props):
            target = props.getProperty('target_path')
            # a failed wheel build must not fail venv creation: the benchmark
            # venv step reports the real error
            return ['bash', '-c',
                    '%s -m venv pyperformance_venv\n'
                    '( %s ) || echo "WARNING: pydantic-core wheel build failed"\n'
                    % (target, build_pydantic_core % {
                        'target': target,
                        'wheelhouse': wheelhouse,
                        'version': pydantic_core_version,
                        'pyo3': pyo3_version})]

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
            env={'PIP_FIND_LINKS': wheelhouse},
            doStepIf=is_py3_target,
            workdir='.'))

        self.addStep(ShellCmd(
            # this step needs exclusive access to the CPU
            locks=[lock.access('exclusive')],
            description="run benchmarks on top of pypy-c",
            command=get_cmd,
            workdir='./benchmarks',
            timeout=3600))

        # Apply every PyPy-compatibility patch in a single step: the
        # renderer reads the patch scripts from the master at build time and
        # ships them inside the command, so the worker needs neither a
        # FileDownload step per patch nor a copy of them on disk.
        _patches_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), '..', '..',
                         'pyperformance-patches'))

        apply_patches_script = """
import base64, json, os, sys, traceback

patches = json.loads(base64.b64decode("@PAYLOAD@").decode("utf-8"))
print("cwd: " + os.getcwd())
print("cwd contents: " + repr(sorted(os.listdir("."))))
if os.path.isdir("venv"):
    print("venv/ contents: " + repr(sorted(os.listdir("venv"))))
else:
    print("venv/ directory does not exist")
print("applying patches: " + repr([name for name, src in patches]))
failed = []
for name, src in patches:
    print("")
    print("--- " + name)
    sys.stdout.flush()
    try:
        exec(compile(src, name, "exec"),
             {"__name__": "__main__", "__file__": name})
    except SystemExit as e:
        if e.code:
            failed.append(name)
    except Exception:
        traceback.print_exc()
        failed.append(name)
    sys.stdout.flush()
if failed:
    sys.exit("FAILED: " + ", ".join(failed))
"""

        @renderer
        def get_apply_patches_cmd(props):
            patches = []
            for path in sorted(glob.glob(
                    os.path.join(_patches_dir, 'patch_*_pypy.py'))):
                with open(path) as f:
                    patches.append((os.path.basename(path), f.read()))
            payload = base64.b64encode(
                json.dumps(patches).encode('utf-8')).decode('ascii')
            return ['python3', '-c',
                    apply_patches_script.replace('@PAYLOAD@', payload)]

        self.addStep(ShellCmd(
            description='apply PyPy compatibility patches',
            command=get_apply_patches_cmd,
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
            description='run pyperformance (nojit)',
            command=get_pyperformance_run_cmd('pyperformance_nojit_result.json',
                                              inherit_environ='PYPY_DISABLE_JIT',
                                              fast=True),
            env={'PYPY_DISABLE_JIT': '1'},
            locks=[lock.access('exclusive')],
            doStepIf=is_py3_target,
            workdir='.',
            timeout=7200))
        self.addStep(UploadCmd(
            description='upload legacy results (jit-on)',
            command=get_upload_baseline_cmd,
            env=upload_env,
            logEnviron=False,
            workdir='.'))
        self.addStep(UploadCmd(
            description='upload legacy results (jit-off)',
            command=get_upload_changed_cmd,
            env=upload_env,
            logEnviron=False,
            workdir='.'))
        self.addStep(UploadCmd(
            description='upload pyperformance results (jit)',
            command=get_pyperformance_upload_cmd,
            env=upload_env,
            logEnviron=False,
            doStepIf=is_py3_target,
            workdir='.'))
        self.addStep(UploadCmd(
            description='upload pyperformance results (nojit)',
            command=get_pyperformance_nojit_upload_cmd,
            env=upload_env,
            logEnviron=False,
            doStepIf=is_py3_target,
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


# The build variant, and the only thing that distinguishes two runs of the
# same CPython version.  Matches the recipe in RUNNING_CPYTHON_BENCHMARKS.md.
DEFAULT_CPYTHON_CONFIGURE_ARGS = (
    '--enable-optimizations --with-lto --with-computed-gotos '
    '--with-openssl=/usr/local --enable-shared '
    'PKG_CONFIG_PATH=/usr/local/lib/pkgconfig')

# where the benchmarked CPython comes from; also the source stamp's repository,
# which is what turns the revision into a link on the web status
DEFAULT_CPYTHON_REPOURL = 'https://github.com/python/cpython'


# Everything CPython uploads goes in under this one codespeed executable.
# Executables are the build-variant axis ('cpython' vs a future 'cpython-lto'),
# not the version axis: the version is already carried by the revision, and a
# per-version executable would mean editing DEF_BASELINES in the codespeed
# settings on every release.
CPYTHON_EXECUTABLE = 'cpython'

# ...and release builds all land on this one codespeed branch, which is where
# the historical 2.7.11 / 3.7.19 / 3.11.9 results already live, so the
# timeline is a single series across releases.  It is a codespeed branch name
# and has nothing to do with the git branch that was checked out.
CPYTHON_RELEASE_BRANCH = '3'

# A released interpreter reports a bare version, '3.12.16'.  patchlevel.h is
# not touched between releases, so every commit after the v3.12.16 tag also
# reports micro 16 at releaselevel 'final' -- sys.version_info cannot tell
# them apart.  PY_VERSION can: the branch carries a trailing '+' ('3.12.16+'),
# and pre-releases carry 'a0' / 'b1' / 'rc2'.  Hence the bare-version match
# below: it is true only for an actual release build.
CPYTHON_RELEASE_VERSION_RE = re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+$')

# What the version probe may return at all.  A probe that returns anything
# else is a broken build, not a non-release build, and must not quietly fall
# back to the sha path.
CPYTHON_ANY_VERSION_RE = r'^[0-9]+\.[0-9]+\.[0-9]+([+]|a[0-9]+|b[0-9]+|rc[0-9]+)?$'


class CPythonBenchmark2(factory.BuildFactory):
    '''
    Build CPython and run both benchmark suites, uploading to codespeed.

    Mirrors JITBenchmark, minus the translation, minus the jit/nojit split, and
    minus the patch_*_pypy.py compatibility patches, which are PyPy-only and
    must not be applied here.

    Properties, all set by CPythonBenchmarkForceScheduler:

      cpython_branch          What gets checked out.  For a non-release build
                              it is also, prefixed with 'cpython', the
                              codespeed branch the results are uploaded under,
                              so the two cannot disagree; a release build goes
                              on CPYTHON_RELEASE_BRANCH instead.  Your own
                              branch name is fine: codespeed creates branches
                              on demand.  The scheduler fills it in from a
                              v3.13.10-style revision if it was left empty.
      cpython_revision        optional tag or sha.  Empty means the branch tip.
                              If given, it is verified to be on the branch
                              before anything is built.
      cpython_configure_args  configure flags, i.e. the build variant.
      benchmark_branch        branch of the legacy benchmarks repo.
      pyperformance_spec      pip specifier for pyperformance.

    The codespeed executable is a constant (CPYTHON_EXECUTABLE), so there is no
    branch-to-executable table to keep in sync.  What a result is uploaded as
    comes from the interpreter that was built: a bare PY_VERSION means a
    release build, which goes in as that version on CPYTHON_RELEASE_BRANCH;
    anything else keeps its sha on cpython_branch, where it cannot collide
    with a release.  See the module constants above.
    '''
    def __init__(self, platform='linux64', host='benchmarker2',
                 cpython_repourl=DEFAULT_CPYTHON_REPOURL,
                 upload_credentials=None):
        factory.BuildFactory.__init__(self)

        update_benchmarks_repo(platform, self)

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

        # matches the recipe: the container's openssl etc. live in /usr/local
        build_env = {'LD_LIBRARY_PATH': '/usr/local/lib64:${LD_LIBRARY_PATH}'}

        # ---------------------------------------------------------------
        # CPython checkout
        # ---------------------------------------------------------------
        self.addStep(ShellCmd(
            description="cpython: clone",
            command="if [ ! -d .git ]; then rm -fr * .[a-z]*; "
                    "git clone %s .; fi" % cpython_repourl,
            workdir='./cpython',
            timeout=3600,
            haltOnFailure=True))
        self.addStep(ShellCmd(
            description="cpython: fetch",
            command=['git', 'fetch', '--tags', '--force', '--prune', 'origin'],
            workdir='./cpython',
            timeout=3600,
            haltOnFailure=True))

        def has_revision(step):
            return bool((step.build.getProperty('cpython_revision')
                         or '').strip())

        @renderer
        def get_ancestry_cmd(props):
            # The whole of the branch-vs-revision problem in one command: a
            # revision that is not on the branch would otherwise be uploaded
            # under it and be indistinguishable from a genuine result.
            rev = (props.getProperty('cpython_revision') or '').strip()
            branch = props.getProperty('cpython_branch')
            return ['git', 'merge-base', '--is-ancestor', rev,
                    'origin/%s' % branch]

        self.addStep(ShellCmd(
            description="cpython: revision is on branch",
            command=get_ancestry_cmd,
            doStepIf=has_revision,
            haltOnFailure=True,
            workdir='./cpython'))

        @renderer
        def get_checkout_cmd(props):
            rev = (props.getProperty('cpython_revision') or '').strip()
            branch = props.getProperty('cpython_branch')
            return ['git', 'checkout', '-f',
                    rev if rev else 'origin/%s' % branch]

        self.addStep(ShellCmd(
            description="cpython: checkout",
            command=get_checkout_cmd,
            haltOnFailure=True,
            workdir='./cpython'))
        self.addStep(ShellCmd(
            description="cpython: clean tree",
            command=['git', 'clean', '-fdx'],
            haltOnFailure=True,
            workdir='./cpython'))

        def extract_revision(rc, stdout, stderr):
            sha = stdout.split()[0]
            return {'cpython_got_revision': sha,
                    'cpython_got_revision_short': sha[:12]}

        self.addStep(shell.SetPropertyFromCommand(
            description="cpython: got_revision",
            command=['git', 'rev-parse', 'HEAD'],
            extract_fn=extract_revision,
            haltOnFailure=True,
            workdir='./cpython'))

        @renderer
        def get_display_revision(props):
            # 'got_revision' is what the web status shows, and links against
            # the source stamp's repository: the tag that was asked for when
            # there was one, since it says more than the sha, and otherwise
            # the sha the branch tip resolved to.  The upload keeps using
            # cpython_got_revision, which is always the sha.
            return ((props.getProperty('cpython_revision') or '').strip()
                    or props.getProperty('cpython_got_revision'))

        self.addStep(MasterSetProperty(
            name="set_got_revision",
            property='got_revision',
            value=get_display_revision))

        # The commit date, not the time we happened to benchmark it.  Codespeed
        # orders a branch's timeline by revision date, and all releases share
        # one branch, so without this the series is in whatever order the
        # builds were run: benchmark 3.12.16 before 3.11.18 and it goes
        # backwards.
        self.addStep(shell.SetPropertyFromCommand(
            description="cpython: revision date",
            command=['git', 'log', '-1', '--format=%cI', 'HEAD'],
            property='cpython_revision_date',
            haltOnFailure=True,
            workdir='./cpython'))

        # ---------------------------------------------------------------
        # Build
        # ---------------------------------------------------------------
        # A prefix inside the builddir, not a shared /opt: two builds of the
        # same version must not overwrite each other, or the results can no
        # longer be told apart.
        self.addStep(shell.SetPropertyFromCommand(
            description="cpython: install prefix",
            command=['python', '-c',
                     "import os; print(os.path.abspath('cpython-install'))"],
            property='cpython_prefix',
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description="cpython: clear install prefix",
            command=Interpolate('rm -rf %(prop:cpython_prefix)s'),
            haltOnFailure=True,
            workdir='.'))

        @renderer
        def get_configure_cmd(props):
            prefix = props.getProperty('cpython_prefix')
            args = (props.getProperty('cpython_configure_args')
                    or DEFAULT_CPYTHON_CONFIGURE_ARGS)
            # $ORIGIN has to reach the linker literally, through three
            # expansions: the single quotes get it past bash -c, configure
            # writes it into the Makefile verbatim, where make turns '$$' into
            # '$' and sh then drops the backslash.  Spelled '$ORIGIN' there,
            # make expands '$O' as an (empty) variable and the rpath becomes
            # 'RIGIN/../lib' -- whereupon the installed python silently loads
            # whatever libpython3.x.so the system happens to have.
            return ['bash', '-c',
                    "./configure --prefix=%s %s "
                    "LDFLAGS='-Wl,-rpath,\$$ORIGIN/../lib "
                    "-Wl,-rpath,/usr/local/lib64 -L/usr/local/lib64'"
                    % (prefix, args)]

        # These steps may run while another build benchmarks; only the
        # measurement steps below take the lock exclusively.
        self.addStep(ShellCmd(
            description="cpython: configure",
            command=get_configure_cmd,
            env=build_env,
            locks=[lock.access('counting')],
            timeout=1800,
            haltOnFailure=True,
            workdir='./cpython'))
        self.addStep(ShellCmd(
            description="cpython: make",
            command=['bash', '-c', 'make -j$(nproc)'],
            env=build_env,
            locks=[lock.access('counting')],
            timeout=7200,
            haltOnFailure=True,
            workdir='./cpython'))
        self.addStep(ShellCmd(
            description="cpython: make install",
            command=['make', 'install'],
            env=build_env,
            locks=[lock.access('counting')],
            timeout=1800,
            haltOnFailure=True,
            workdir='./cpython'))

        @renderer
        def get_pip_cmd(props):
            return [props.getProperty('cpython_prefix') + '/bin/python3',
                    '-m', 'pip', 'install', 'setuptools<70']

        self.addStep(ShellCmd(
            description="cpython: pip install setuptools",
            command=get_pip_cmd,
            env=build_env,
            haltOnFailure=True,
            workdir='.'))

        # PY_VERSION, read off the built interpreter.  sys.version's leading
        # token is PY_VERSION verbatim, trailing '+' and all; the '+' is what
        # separates a release build from a later commit on the same branch, so
        # do not go through sys.version_info here -- it drops the '+'.
        @renderer
        def get_version_probe_cmd(props):
            return [props.getProperty('cpython_prefix') + '/bin/python3', '-c',
                    "import sys; print(sys.version.split()[0])"]

        self.addStep(shell.SetPropertyFromCommand(
            description="cpython: version",
            command=get_version_probe_cmd,
            property='cpython_version',
            env=build_env,
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description="cpython: check version",
            command=Interpolate(
                'echo "%(prop:cpython_version)s" | grep -Eq '
                "'" + CPYTHON_ANY_VERSION_RE + "'"),
            haltOnFailure=True,
            workdir='.'))

        # ---------------------------------------------------------------
        # Legacy suite
        # ---------------------------------------------------------------
        # run_local.py, not runner.py directly: it also records the 'metadata'
        # dict (configure line, mtime, cpu affinity) that nothing else does.
        # It puts the real timings in base_times, hence --baseline on upload.
        @renderer
        def get_legacy_run_cmd(props):
            python = props.getProperty('cpython_prefix') + '/bin/python3'
            return [python, 'run_local.py', python, '-o', 'result.json']

        self.addStep(ShellCmd(
            # this step needs exclusive access to the CPU
            locks=[lock.access('exclusive')],
            description="run legacy benchmarks on top of cpython",
            command=get_legacy_run_cmd,
            env=build_env,
            workdir='./benchmarks',
            haltOnFailure=True,
            timeout=7200))

        # ---------------------------------------------------------------
        # pyperformance suite
        # ---------------------------------------------------------------
        @renderer
        def get_pyperformance_venv_cmd(props):
            python = props.getProperty('cpython_prefix') + '/bin/python3'
            return ['bash', '-c', 'rm -rf pyperformance_venv && '
                                  '%s -m venv pyperformance_venv' % python]

        @renderer
        def get_pyperformance_install_cmd(props):
            spec = props.getProperty('pyperformance_spec',
                                     default='pyperformance').strip()
            return ['./pyperformance_venv/bin/pip', 'install', '--upgrade', spec]

        @renderer
        def get_pyperformance_bench_venv_cmd(props):
            return ['./pyperformance_venv/bin/python', '-m', 'pyperformance',
                    'venv', 'recreate', '-p',
                    props.getProperty('cpython_prefix') + '/bin/python3']

        @renderer
        def get_pyperformance_run_cmd(props):
            python = props.getProperty('cpython_prefix') + '/bin/python3'
            return ['bash', '-c',
                    'rm -f pyperformance_result.json && '
                    './pyperformance_venv/bin/python -m pyperformance run '
                    '--python %s --output pyperformance_result.json' % python]

        self.addStep(ShellCmd(
            description='create pyperformance venv',
            command=get_pyperformance_venv_cmd,
            env=build_env,
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description='install pyperformance',
            command=get_pyperformance_install_cmd,
            env=build_env,
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description='create pyperformance benchmark venv',
            command=get_pyperformance_bench_venv_cmd,
            env=build_env,
            haltOnFailure=True,
            workdir='.'))
        self.addStep(ShellCmd(
            description='run pyperformance',
            command=get_pyperformance_run_cmd,
            env=build_env,
            locks=[lock.access('exclusive')],
            haltOnFailure=True,
            workdir='.',
            timeout=14400))

        # ---------------------------------------------------------------
        # Upload
        # ---------------------------------------------------------------
        self.addStep(transfer.FileDownload(
            mastersrc=os.path.join(os.path.dirname(__file__), 'bulk_upload.py'),
            slavedest='bulk_upload.py',
            workdir='.'))

        def _props_for_upload(props):
            """Return the (revision, codespeed branch) to upload under.

            A release build is identified by its own PY_VERSION and goes in as
            that version on CPYTHON_RELEASE_BRANCH, giving the comparison page
            one entry per release and the timeline one series across them.

            Anything else -- a post-release branch build, a pre-release, a
            personal branch -- keeps its sha on its own branch, codespeed
            naming it 'cpython3.13' where the git branch is '3.13'.  It has to:
            every commit between two releases reports the earlier release's
            version, so uploading those as the version would land them on the
            same (commitid, branch) as the release itself, and codespeed
            overwrites a revision's results in place rather than rejecting the
            duplicate.  The sha path also keeps the uploaded branch in step
            with the checked-out one, which is what the ancestry check above is
            there to enforce.
            """
            version = (props.getProperty('cpython_version') or '').strip()
            if CPYTHON_RELEASE_VERSION_RE.match(version):
                return version, CPYTHON_RELEASE_BRANCH
            return (props.getProperty('cpython_got_revision'),
                    'cpython' + props.getProperty('cpython_branch'))

        def _upload_cmd(props, jsonfile, extra=()):
            rev, branch = _props_for_upload(props)
            cmd = ['python3', 'bulk_upload.py', jsonfile,
                   '-e', CPYTHON_EXECUTABLE,
                   '-H', upload_env['SPEED_UPLOAD_HOST'],
                   '-P', 'cpython', '-r', rev, '-B', branch,
                   '-u', upload_env['SPEED_UPLOAD_URL']]
            rev_date = props.getProperty('cpython_revision_date')
            if rev_date:
                cmd += ['-d', rev_date]
            return cmd + list(extra)

        @renderer
        def get_legacy_upload_cmd(props):
            return _upload_cmd(props, 'benchmarks/result.json',
                               extra=['--baseline'])

        @renderer
        def get_pyperformance_upload_cmd(props):
            return _upload_cmd(props, 'pyperformance_result.json')

        self.addStep(UploadCmd(
            description='upload legacy results',
            command=get_legacy_upload_cmd,
            env=upload_env,
            logEnviron=False,
            workdir='.'))
        self.addStep(UploadCmd(
            description='upload pyperformance results',
            command=get_pyperformance_upload_cmd,
            env=upload_env,
            logEnviron=False,
            workdir='.'))

        # ---------------------------------------------------------------
        # Archive the raw result files on the master
        # ---------------------------------------------------------------
        # Codespeed keeps one aggregate value per benchmark; the per-iteration
        # timings, std devs and run metadata only exist in these files.
        filename = 'cpython-%(cpython_got_revision_short)s'
        resfile = os.path.expanduser("~/bench_results/%s-%s.json" % (filename, host))
        self.addStep(transfer.FileUpload(slavesrc="benchmarks/result.json",
                                         masterdest=WithProperties(resfile),
                                         workdir="."))
        pyresfile = os.path.expanduser(
            "~/bench_results/%s-pyperformance.json" % filename)
        self.addStep(transfer.FileUpload(slavesrc="pyperformance_result.json",
                                         masterdest=WithProperties(pyresfile),
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
