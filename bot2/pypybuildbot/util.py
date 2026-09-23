import os
import socket
import subprocess
from twisted.python import log

def we_are_debugging():
    return True

def load(name):
    mod = __import__(name, {}, {}, ['__all__'])
    reload(mod)
    return mod

def symlink_force(src, dst):
    """
    More or less equivalent to "ln -fs": it overwrites the destination, if it
    exists
    """
    if os.path.lexists(dst):
        os.remove(dst)
    os.symlink(src, dst)

def get_master_commit():
    """
    Describe the git commit of this buildbot2 checkout, for the about page.

    Call this while master.cfg is being loaded, not once per request: what we
    want to show is the revision the running master actually picked up, which
    only changes when it is restarted or reconfig'd -- not whatever happens to
    be in the checkout at the moment somebody looks at the page.

    Returns a dict with 'commit', 'date', 'subject' and 'dirty', or None if
    this is not a git checkout (or git is not available).
    """
    # util.py lives in <repo>/bot2/pypybuildbot/
    repodir = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def git(*args):
        proc = subprocess.Popen(('git',) + args, cwd=repodir,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError('git %s failed: %s' %
                               (' '.join(args), stderr.strip()))
        return stdout.strip()

    try:
        commit, date, subject = git(
            'log', '-1', '--format=%h%n%ci%n%s').split('\n', 2)
        # --untracked-files=no: the master writes all sorts of things into the
        # checkout, only tracked files tell us whether the code was modified
        dirty = bool(git('status', '--porcelain', '--untracked-files=no'))
    except Exception, e:
        log.msg('could not determine the buildbot2 git commit: %s' % (e,))
        return None
    return {'commit': commit, 'date': date, 'subject': subject,
            'dirty': dirty}

def isRPython(change):
    for fname in change.files:
        if fname.startswith('rpython'):
            log.msg('fileIsImportant filter isRPython got "%s"' % fname)
            return True
    return False
