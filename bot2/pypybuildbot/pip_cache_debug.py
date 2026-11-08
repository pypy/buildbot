"""Debug helper for pip's http cache, used by the win32 test factory.

'instrument' makes the venv's pip report a cache write error instead of
dropping it, 'report' lists writes that never got renamed into place and
entries whose body does not match the stored content-length, and 'repair'
deletes those.  A mismatched entry is not self-healing: pip serves the short
body against the stored content-length, and urllib3 2.x raises IncompleteRead
on it in every later run (issue 5713).  All modes always exit 0: this is
diagnostics, it must never fail a build.

The optional second argument is the cache directory, for testing.
"""
import os
import sys

OLD = """    except OSError:
        pass
"""
NEW = """    except OSError:
        import sys, traceback
        sys.stderr.write("BUILDBOT: pip cache write failed\\n")
        traceback.print_exc()
"""


def cache_module():
    from pip._internal.network import cache
    return cache.__file__.replace(".pyc", ".py")


def instrument():
    path = cache_module()
    with open(path) as f:
        source = f.read()
    if NEW in source:
        print("already instrumented: %s" % path)
        return
    if source.count(OLD) != 1:
        print("ERROR: pattern not found in %s, pip version changed?" % path)
        return
    with open(path, "w") as f:
        f.write(source.replace(OLD, NEW))
    print("instrumented %s" % path)


def cache_dir():
    from pip._internal.locations import USER_CACHE_DIR
    return os.path.join(USER_CACHE_DIR, "http-v2")


def stored_length(path):
    from pip._vendor import msgpack
    with open(path, "rb") as f:
        _version, _, payload = f.read().partition(b",")
    headers = msgpack.loads(payload, raw=False)["response"]["headers"]
    for key, value in headers.items():
        if key.lower() == "content-length" and value.isdigit():
            return int(value)
    return None


def body_mismatch(path):
    """Return (content-length, body size) if the entry at 'path' has a body
    that does not match the stored content-length, else None
    """
    body = path + ".body"
    if not os.path.exists(body):
        return None
    try:
        want = stored_length(path)
    except Exception as e:
        print("UNREADABLE %s: %s" % (os.path.basename(path), e))
        return None
    if want is None:
        return None
    size = os.path.getsize(body)
    if want != size:
        return want, size
    return None


def remove(path):
    try:
        os.remove(path)
        return True
    except OSError as e:
        print("could not remove %s: %s" % (path, e))
        return False


def repair(directory=None):
    """Delete what a failed body write left behind, so that the next pip run
    downloads the entry again instead of failing on it
    """
    directory = directory or cache_dir()
    print("pip cache: %s" % directory)
    entries = dropped = 0
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            path = os.path.join(root, name)
            if name.endswith(".tmp"):
                if remove(path):
                    dropped += 1
                continue
            if name.endswith(".body"):
                continue
            mismatch = body_mismatch(path)
            if mismatch is None:
                continue
            want, size = mismatch
            print("REPAIR %s: content-length=%d body=%d (%+d)"
                  % (name, want, size, size - want))
            remove(path + ".body")
            if remove(path):
                entries += 1
    print("removed %d inconsistent entries, %d unfinished writes"
          % (entries, dropped))


def report(directory=None):
    import time
    directory = directory or cache_dir()
    print("pip cache: %s" % directory)
    unfinished = mismatched = entries = 0
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            path = os.path.join(root, name)
            if name.endswith(".tmp"):
                unfinished += 1
                print("UNFINISHED %s\n    %d bytes, %s"
                      % (name, os.path.getsize(path),
                         time.ctime(os.path.getmtime(path))))
                continue
            if name.endswith(".body"):
                continue
            body = path + ".body"
            if not os.path.exists(body):
                continue
            entries += 1
            mismatch = body_mismatch(path)
            if mismatch is not None:
                want, size = mismatch
                mismatched += 1
                print("MISMATCH %s\n    content-length=%d body=%d (%+d)"
                      % (name, want, size, size - want))
                print("    meta %s / body %s"
                      % (time.ctime(os.path.getmtime(path)),
                         time.ctime(os.path.getmtime(body))))
    print("%d entries, %d mismatched, %d unfinished writes"
          % (entries, mismatched, unfinished))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    directory = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        if mode == "instrument":
            instrument()
        elif mode == "repair":
            repair(directory)
        else:
            report(directory)
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
