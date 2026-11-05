"""Debug helper for pip's http cache, used by the win32 test factory.

'instrument' makes the venv's pip report a cache write error instead of
dropping it, 'report' lists writes that never got renamed into place and
entries whose body does not match the stored content-length.  Both modes
always exit 0: this is diagnostics, it must never fail a build.
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


def report():
    import time
    directory = cache_dir()
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
            try:
                want = stored_length(path)
            except Exception as e:
                print("UNREADABLE %s: %s" % (name, e))
                continue
            size = os.path.getsize(body)
            if want is not None and want != size:
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
    try:
        if mode == "instrument":
            instrument()
        else:
            report()
    except Exception:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
