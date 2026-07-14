import time, datetime, urlparse, urllib
import operator

import py
html = py.xml.html

from buildbot.status.web.base import HtmlResource
from buildbot.status.builder import FAILURE, EXCEPTION

def host_agnostic(url):
    parts = urlparse.urlsplit(url)
    return urlparse.urlunsplit(('','')+parts[2:])

def show_elapsed(secs):
    if secs < 5:
        return "%.02fs" % secs
    secs = int(round(secs))
    if secs < 60:
        return "%ds" % secs
    if secs < 5*60:
        return "%dm%d" % (secs/60, secs%60)
    mins = int(round(secs/60.))
    if mins < 60:
        return "%dm" % mins
    return "%dh%d" % (mins/60, mins%60)

class OutcomeSummary(object):
    def __init__(self, p, F, s, x):
        self.p = p # passed
        self.F = F # failed
        self.s = s # skipped
        self.x = x # xfailed

    def is_ok(self):
        return self.F == 0

    def to_tuple(self):
        return self.p, self.F, self.s, self.x

    def __str__(self):
        return '%d, %d F, %d s, %d x' % (self.p, self.F, self.s, self.x)

    def __add__(self, other):
        return self.__class__(self.p + other.p,
                              self.F + other.F,
                              self.s + other.s,
                              self.x + other.x)

class RevisionOutcomeSet(object):

    def __init__(self, rev, key=None, run_info=None):
        self.revision = rev
        self.key = key
        self._outcomes = {}
        self.failed = set()
        self.skipped = set()
        self._xfailed = 0
        self._xpassed = 0
        self.longreprs = {}
        self._run_info = run_info

    def get_summary(self):
        return OutcomeSummary(self.numpassed,
                              len(self.failed),
                              len(self.skipped),
                              self.numxfailed)

    def populate_one(self, name, shortrepr, longrepr=None):
        if shortrepr == '!':
            namekey = [name, '']
        else:
            # pytest2 and pytest1 use different separators/test id
            # syntax support both here for now
            if '.py::' in name:
                namekey = name.split('::', 1)
            else:
                namekey = name.split(':', 1)
            if namekey[0].endswith('.py'):
                namekey[0] = namekey[0][:-3].replace('/', '.')
            if len(namekey) == 1:
                namekey.append('')
            namekey[1] = namekey[1].replace("::", ".")

        namekey = tuple(namekey)
        self._outcomes[namekey] = shortrepr
        if shortrepr.lower() == 's':
            self.skipped.add(namekey)
        elif shortrepr == '.':
            pass
        elif shortrepr == 'x':
            self._xfailed += 1
        elif shortrepr == 'X':
            self._xpassed += 1
        else:
            self.failed.add(namekey)

        if longrepr:
            try:
                longrepr = longrepr.decode("utf-8")
            except UnicodeDecodeError:
                longrepr = longrepr.decode("latin-1")

            self.longreprs[namekey] = longrepr

    @property
    def numpassed(self):
        return (len(self._outcomes) - len(self.skipped) - len(self.failed) -
                self._xfailed - self._xpassed)

    @property
    def numxfailed(self):
        return self._xfailed

    def populate(self, log):
        kind = None
        def add_one():
            if kind is not None:
                self.populate_one(name, kind, ''.join(longrepr))
        for line in log.readlines():
            first = line[0]
            if first == ' ':
                longrepr.append(line[1:])
                continue
            add_one()
            kind = first
            name = line[2:].rstrip()
            longrepr = []
        add_one()

    def populate_xml(self, log):
        import xml.etree.ElementTree as ET
        tree = ET.fromstring(log.getText())
        if tree.tag != "testsuite":
            # Could be a "testsuites" node around the "testsuite"
            tree = tree.find("testsuite")
        for item in tree:
            if item.tag != "testcase":
                continue
            failures = item.findall("failure")
            skipped = item.findall("skipped")
            errors = item.findall("error")
            if errors:
                longrepr = errors[0].text.encode('utf-8')
                kind = "E"
            elif failures:
                longrepr = failures[0].text.encode('utf-8')
                kind = "F"
            elif skipped:
                t = skipped[0].get("type", "")
                if "xfail" in t:
                    kind = 'x'
                elif "xpass" in t:
                    kind = "X"
                else:
                    kind = "s"
                longrepr = skipped[0].get("message", "")
            else:
                kind = "."
                longrepr = ""
            name = ":".join([item.get("classname", ""), item.get("name", "")])
            self.populate_one(name, kind, longrepr)

    def get_outcome(self, namekey):
        return self._outcomes.get(namekey, ' ')

    def get_longrepr(self, namekey):
        return self.longreprs.get(namekey, '')

    def get_key_namekey(self, namekey):
        return (self.key, namekey)

    def get_run_infos(self):
        return {self.key: (self, self._run_info)}


class RevisionOutcomeSetCache(object):

    def __init__(self, cachesize=10):
        self._outcome_sets = {}
        self._lru = []
        self._hits = 0
        self._misses = 0
        self.cachesize = cachesize

    def clear(self): # for testing
        self._outcome_sets = {}
        self._lru = []

    def reset_counters(self):
        self._hits = 0
        self._misses = 0

    def stats(self):
        return "hits: %d, misses: %d" % (self._hits, self._misses)

    def _load_outcome_set(self, status, key):
        builderName, buildNumber = key
        builderStatus = status.getBuilder(builderName)
        build = builderStatus.getBuild(buildNumber)
        run_url = status.getURLForThing(build)

        rev = build.getProperty("got_revision")
        pytest_logs = []
        pytest_elapsed = 0
        with_logs = set()
        for step in build.getSteps():
            logs = dict((log.getName(), log) for log in step.getLogs())
            if 'pytestLog' in logs:
                with_logs.add(step)
                pytest_logs.append((step.getName(), logs['pytestLog']))
                ts = step.getTimes()
                if ts[0] is not None and ts[1] is not None:
                    pytest_elapsed += ts[1]-ts[0]

        run_info = {'URL': run_url, 'elapsed': pytest_elapsed or None,
                    'times': build.getTimes()}
        outcome_set = RevisionOutcomeSet(rev, key, run_info)
        #someresult = False
        ## "*-run" categories mean the build is not a test build!
        #if builderStatus.category:
        #    someresult = builderStatus.category.endswith("-run")
        if pytest_logs:
            for stepName, resultLog in pytest_logs:
                if resultLog.hasContents():
                    if resultLog.getText().startswith("<?xml "):
                        outcome_set.populate_xml(resultLog)
                    else:
                        outcome_set.populate(resultLog)

        failedtests = not not outcome_set.failed

        failure = None
        for step in build.getSteps():
            if step.getResults()[0] in (FAILURE, EXCEPTION):
                text = ' '.join(step.getText())
                if step in with_logs:
                    if failedtests and text.endswith('failed'):
                        continue
                failure = text
                break

        #if not someresult or failure is not None:
        if failure is not None:
            #if failure:
            name = '"%s"' % failure # quote
            #else:
            #    name = '<run>'
            outcome_set.populate_one(name, '!')

        return outcome_set

    def get(self, status, key):
        try:
            self._lru.remove(key)
        except ValueError:
            pass
        self._lru.append(key)
        try:
            outcome_set = self._outcome_sets[key]
            self._hits += 1
            return outcome_set
        except KeyError:
            pass
        self._misses += 1
        if len(self._lru) > self.cachesize:
            dead_key = self._lru.pop(0)
            self._outcome_sets.pop(dead_key, None)
        outcome_set = self._load_outcome_set(status, key)
        self._outcome_sets[key] = outcome_set
        return outcome_set

class GatherOutcomeSet(object):

    def __init__(self, map):
        self.map = map
        self._failed = None
        self._skipped = None
        self._numpassed = None
        self._numxfailed = None
        self.revision = map.values()[0].revision

    @property
    def failed(self):
        if self._failed is None:
            self._failed = set()
            for prefix, outcome in self.map.items():
                self._failed.update([(prefix,) + namekey for namekey in
                                     outcome.failed])
        return self._failed

    @property
    def skipped(self):
        if self._skipped is None:
            self._skipped = set()
            for prefix, outcome in self.map.items():
                self._skipped.update([(prefix,) + namekey for namekey in
                                     outcome.skipped])
        return self._skipped

    @property
    def numpassed(self):
        if self._numpassed is None:
            numpassed = 0
            for  prefix, outcome in self.map.items():
                numpassed += outcome.numpassed
            self._numpassed = numpassed
        return self._numpassed

    @property
    def numxfailed(self):
        if self._numxfailed is None:
            numxfailed = 0
            for  prefix, outcome in self.map.items():
                numxfailed += outcome.numxfailed
            self._numxfailed = numxfailed
        return self._numxfailed

    def get_outcome(self, namekey):
        which = namekey[0]
        if which not in self.map:
            return ' '
        return self.map[which].get_outcome(namekey[1:])

    def get_longrepr(self, namekey):
        which = namekey[0]
        if which not in self.map:
            return ''
        return self.map[which].get_longrepr(namekey[1:])

    def get_key_namekey(self, namekey):
        return self.map[namekey[0]].get_key_namekey(namekey[1:])

    def get_run_infos(self):
        all = {}
        for outcome_set in self.map.itervalues():
            all.update(outcome_set.get_run_infos())
        return all

# ________________________________________________________________

N = 5

outcome_set_cache = RevisionOutcomeSetCache(32*(N+1))


def colsizes(namekeys):
    colsizes = None
    for keys in namekeys:
        if colsizes is None:
            colsizes = [0] * len(keys)
        colsizes = map(max, zip(map(len, keys), colsizes))

    return colsizes


class SummaryPage(object):
    SUCCESS_LINE = True

    def __init__(self, status):
        self.sections = []
        self.cur_cat_branch=None
        self.n_builders = 0
        self.status = status

    def make_longrepr_url_for(self, outcome_set, namekey):
        cachekey, namekey = outcome_set.get_key_namekey(namekey)
        parms={
            'builder': cachekey[0],
            'build': cachekey[1],
            'mod': namekey[0],
            'testname': namekey[1]
            }
        qs = urllib.urlencode(parms)
        return "/summary/longrepr?" + qs

    def make_run_anchors_for(self, outcome_set):
        anchors = []
        infos = sorted(outcome_set.get_run_infos().items())
        minend = None
        maxend = None
        for cachekey, (run, info) in infos:
            builder = cachekey[0]
            anchors.append('  ')
            timing = ""
            if self.n_builders == 1 and info['elapsed'] is not None:
                timing = " in %s" % show_elapsed(info['elapsed'])
            if info['times'][1] is not None:
                day = time.localtime(info['times'][1])[:3]
                if minend is None:
                    minend = day
                else:
                    minend = min(minend, day)
                if maxend is None:
                    maxend = day
                else:
                    maxend = max(maxend, day)
            text = "%s [%d, %d F, %d s, %d x%s]" % (builder,
                                                    run.numpassed,
                                                    len(run.failed),
                                                    len(run.skipped),
                                                    run.numxfailed,
                                                    timing)
            anchors.append(html.a(text, href=host_agnostic(info['URL'])))
        if maxend is not None:
            mintxt = datetime.date(*minend).strftime("%d %b")
            maxtxt = datetime.date(*maxend).strftime("%d %b")
            if maxend == minend:
                anchors.append(' (%s)' % maxtxt)
            else:
                anchors.append(' (%s..%s)' % (mintxt, maxtxt))
        return anchors

    def _start_cat_branch(self, cat_branch, fine=False):
        category, branch = cat_branch
        branch = meta_branch_name(branch)
        category = category_name(category)

        self.cur_cat_branch = (category, branch)

        cat_anchor = html.a("{%s}" % category,
                            href="/summary?category=%s" % category,
                            class_="failSummary branch")

        branch_anchor = html.a(branch,
                               href="/summary?branch=%s" % branch,
                               class_="failSummary branch")
        if fine:
            extra = html.img(alt=":-)", src="success.png")
        else:
            extra = ""
        self.sections.append(html.h2(cat_anchor," ",branch_anchor, " ", extra))

    def _builder_anchor(self, builder):
        if self.n_builders > 0:
            url = self.status.getURLForThing(self.status.getBuilder(builder))
            cls = "builder"
        else:
            url = "/summary?builder=%s" % builder
            cls = "builderquery"
        return html.a(builder, href=host_agnostic(url),
                      class_=' '.join(["failSummary", cls]))

    def _builder_num(self, outcome_set):
        return outcome_set.map.values()[0].key

    def _label(self, outcome_set):
        if self.n_builders > 0:
            # (rev, buildNumber)
            buildNumber = self._builder_num(outcome_set)[1]
            return (outcome_set.revision, buildNumber)
        else:
            # rev
            return outcome_set.revision

    def _label_for_sorting(self, outcome_set):
        encodedrev = encode_rev_for_ordering(outcome_set.revision)
        if self.n_builders > 0:
            # (rev, buildNumber)
            buildNumber = self._builder_num(outcome_set)[1]
            return (encodedrev, buildNumber)
        else:
            # rev
            return encodedrev

    def _label_anchor(self, outcome_set, revsize):
        rev = outcome_set.revision
        if self.n_builders > 0:
            pick = "builder=%s&builds=%d" % self._builder_num(outcome_set)
        else:
            pick = "recentrev=%s" % rev
        category, branch = self.cur_cat_branch
        revtxt = str(rev)
        rev_anchor = html.a(revtxt, href="/summary?category=%s&branch=%s&%s" %
                            (category, branch, pick))
        rightalign = ' '*(revsize-len(revtxt))
        return [rev_anchor, rightalign]

    def add_section(self, cat_branch, outcome_sets):
        if not outcome_sets:
            return
        outcome_sets.sort(key=self._label_for_sorting)
        labels = [self._label(outcome_set) for outcome_set in outcome_sets]
        by_label = [(self._label(outcome_set), outcome_set)
                    for outcome_set in outcome_sets]
        revs = [outcome_set.revision for outcome_set in outcome_sets]

        _, last = by_label[-1]
        self._start_cat_branch(cat_branch, fine = not last.failed)

        lines = []
        revsize = max(map(len, revs))
        align = 2*len(labels)-1+revsize
        def bars():
            return ' |'*len(lines)
        for label, outcome_set in by_label:
            line = [bars(), ' '] + self._label_anchor(outcome_set, revsize)
            line.append((align-len(line[0]))*" ")
            line.append(self.make_run_anchors_for(outcome_set))
            line.append('\n')
            lines.append(line)
        lines.append([bars(), "\n"])

        a_num = len(self.sections)
        if self.SUCCESS_LINE:
            success = []
            for label, outcome_set in by_label:
                if outcome_set.failed:
                    symbol = html.a("-",
                        id="a%dc%d" % (a_num, 1<<len(success)),
                        href="javascript:togglestate(%d,%d)" % (
                                       a_num, 1<<len(success)),
                        class_="failSummary failed")
                else:
                    symbol = html.span("+", class_="failSummary success")
                success.append([" ", symbol])
            success.append("  success\n")
            lines.append(success)

        failed = set()
        exploded = set()
        for label, outcome_set in by_label:
            for failure in outcome_set.failed:
                letter = outcome_set.get_outcome(failure)
                if letter == '!':
                    exploded.add(failure)
                failed.add(failure)

        colwidths = colsizes(failed)

        def sorting(x):
            return (x not in exploded, x)

        for failure in sorted(failed, key=sorting):
            line = []
            combination = 0
            for i, (label, outcome_set) in enumerate(by_label):
                letter = outcome_set.get_outcome(failure)
                failed = letter.lower() not in ('s', '.', ' ', 'x')
                if failed:
                    combination |= 1 << i
                if outcome_set.get_longrepr(failure):
                    longrepr_url = self.make_longrepr_url_for(outcome_set,
                                                              failure)
                    extra = {}
                    if failed:
                        extra = {'class': "failSummary failed"}
                    elif letter in (' ', '.'):
                        # failure, but letter is " " or ".".
                        # Replace with "E", which stands out more
                        letter = 'E'
                    line.append([" ",html.a(letter, href=longrepr_url,
                                            **extra)])
                else:
                    if failed:
                        line.append([" ",
                                     html.span(letter,
                                               class_="failSummary failed")])
                    else:
                        line.append(" %s" % letter)
            # builder
            builder_width = colwidths[0]
            builder = failure[0]
            spacing = ("  %-*s" % (builder_width, 'x'*len(builder))).split('x')
            spaceleft = spacing[0]
            spaceright = spacing[-1]
            builder_anchor = self._builder_anchor(builder)
            line.append([spaceleft, builder_anchor, spaceright])

            for width, key in zip(colwidths[1:], failure[1:]):
                line.append("  %-*s" % (width, key))
            line.append('\n')
            lines.append(html.span(line,
                                   class_="a%dc%d" % (a_num, combination)))

        section = html.nobr(html.pre(lines))
        self.sections.append(section)

    def add_no_revision_builds(self, status, no_revision_builds):
        if not no_revision_builds:
            return
        section = html.div(html.p("builds aborted without getting"
                                  " a revision:"))

        for build in no_revision_builds:
            builderName = build.getBuilder().getName()
            num = build.getNumber()
            descr = "%s #%d" % (builderName, num)
            url = status.getURLForThing(build)
            section.append(html.a(descr, href=host_agnostic(url)))
            section.append(html.br())
        self.sections.append(section)

    def add_comment(self, comm):
        self.sections.append(py.xml.raw("<!-- %s -->" % comm))

    def render(self):
        body_html = html.div(self.sections)
        return body_html.unicode()

class LongRepr(HtmlResource):

    def get_namekey(self, request):
        mod = request.args.get('mod', [])
        if not mod:
            mod = None
        else:
            mod = mod[0]

        testname = request.args.get('testname', [])
        if testname:
            testname = testname[0]
        else:
            testname = ''

        return (mod, testname)

    def getTitle(self, request):
        mod, testname = self.get_namekey(request)
        if mod is None:
            return "no such test"
        return "%s %s" % (mod, testname)

    def body(self, request):
        t0 = time.time()
        outcome_set_cache.reset_counters()

        builder = request.args.get('builder', [])
        build = request.args.get('build', [])
        if not builder or not build:
            return "no such build"
        builderName = builder[0]
        buildNumber = int(build[0])

        outcome_set = outcome_set_cache.get(self.getStatus(request),
                                            (builderName,
                                             buildNumber))

        namekey = self.get_namekey(request)

        longrepr = outcome_set.get_longrepr(namekey)

        builderLink = html.a('builder: %s build #%s' % (builderName, buildNumber),
                             href="/builders/%s/builds/%s" % (
                                 builderName, buildNumber))

        return html.div([html.h2(self.getTitle(request)),
                         html.pre(longrepr),
                         html.pre(builderLink,
                                  style='border-top:1px solid;'),
                         html.pre('test: ' + self.getTitle(request).replace('.','/')),
                         py.xml.raw("<!-- %s -->" % outcome_set_cache.stats())
                         ]).unicode()

def getProp(obj, name, default=None):
    try:
        return obj.getProperty(name)
    except KeyError:
        return default

def make_test(lst):
    if lst is None:
        return lambda v: True
    else:
        membs = set(lst)
        return lambda v: v in membs

def make_subst(v1, v2):
    if not isinstance(v1, list):
        v1 = [v1]
    def subst(v):
        if v in v1:
            return v2
        return v
    return subst

# Map certain branch names from SourceStamps to a common name shown on the page
meta_branch_name = make_subst(['default', '', None], 'main')
# map the meta-branch main to the actual branch entries from the
# SourceStamp
default_value = make_subst('main', ['main', '', None, 'default'])
category_name = make_subst(None, '-')
nocat_value = make_subst("-", None)

def safe_int(v):
    try:
        return int(v)
    except ValueError:
        return None

def encode_rev_for_ordering(rev):
    # subversion: just an integer
    if isinstance(rev, int) or rev.isdigit():
        return (1, int(rev))
    # mercurial: "integer:globalid"
    if ':' in rev and rev[:rev.index(':')].isdigit():
        i = rev.index(':')
        return (2, int(rev[:i]), rev)
    # unknown
    return (0, rev)

class Summary(HtmlResource):

    PLATFORM_PRIORITY = {
        'linux64':       1,
        'linux32':       2,
        'aarch64':       3,
        'macos-x86_64':  4,
        'macos-arm64':   4,
        'win32':         5,
        'win64':         5,
        'linux-s390x':   6,
        'linux-armhf':   7,
        'benchmark-run': 8,
        }

    def __init__(self, categories=[], branch_order_prefixes=[]):
        HtmlResource.__init__(self)
        self.putChild('longrepr', LongRepr())
        self._defaultBranchCache = {}
        self.categories = categories
        self.branch_order_prefixes = branch_order_prefixes

    def content(self, req, context):
        body = self.body(req)
        context['content'] = body
        template = req.site.buildbot_service.templates.get_template(
            "summary.html")
        return template.render(**context)

    def getTitle(self, request):
        status = self.getStatus(request)
        return "%s: summaries of last %d revisions" % (status.getTitle(), N)

    @staticmethod
    def _prune_runs(runs, cutnum):
        keys = runs.keys()
        #
        def revkey(rev):
            if isinstance(rev, tuple):
                buildNumber, rev = rev
            else:
                buildNumber = None
            return (buildNumber, encode_rev_for_ordering(rev))
        #
        keys.sort(key=revkey)
        if len(runs) > cutnum:
            for rev in keys[:-cutnum]:
                del runs[rev]

    def _fish_defaultBranch(self, status, builderName):
        try:
            return self._defaultBranchCache[builderName]
        except KeyError:
            pass
        builder = status.botmaster.builders[builderName]
        factory = builder.config.factory
        branch = None
        for step in factory.steps:
            kw = step.kwargs
            if 'defaultBranch' in kw:
                if kw.get('explicitBranch'):
                    branch = kw['defaultBranch']
                break
        self._defaultBranchCache[builderName] = branch
        return branch

    def _get_branch(self, status, build):
        branch = getProp(build, 'explicitBranch')
        # fish
        if branch is None:
            builderName = build.getBuilder().getName()
            branch = self._fish_defaultBranch(status, builderName)
            branch = branch or getProp(build, 'branch')
        return branch

    def _now(self):
        return time.time()

    def _age(self, build):
        start, _ = build.getTimes()
        return (self._now()-start)/(60*60*24) # in days

    def recentRuns(self, status, only_recentrevs=None, only_branches=None,
                                 only_builder=None, only_builds=None,
                                 only_categories=None):
        test_rev = make_test(only_recentrevs)
        test_branch = make_test(only_branches)
        test_builder = make_test(only_builder)
        n_builders = len(only_builder) if only_builder else 0
        prune_old = not (only_builds or only_recentrevs or
                         only_builder or only_branches)

        cat_branches = {}
        for builderName in status.getBuilderNames(only_categories):
            if not test_builder(builderName):
                continue
            builderStatus = status.getBuilder(builderName)
            if only_builds:
                def builditer():
                    for num in only_builds:
                        b = builderStatus.getBuild(num)
                        if b is not None:
                            yield b
                builditer = builditer()
            else:
                builditer = builderStatus.generateFinishedBuilds(num_builds=5*N)

            for build in builditer:
                if prune_old and self._age(build) > 14:
                    continue
                if self._age(build) > 120:   # four months old: prune anyway
                    continue
                branch = self._get_branch(status, build)
                if not test_branch(branch):
                    continue
                got_rev = getProp(build, 'got_revision', None)
                if not test_rev(got_rev):
                    continue

                branch_ = meta_branch_name(branch)
                cat_branch = (builderStatus.category, branch_)

                runs, no_revision_builds = cat_branches.setdefault(cat_branch,
                                                               ({}, []))

                if got_rev is None:
                    if self._age(build) <= 7:
                        no_revision_builds.append(build)
                else:
                    rev = got_rev
                    buildNumber = build.getNumber()
                    if n_builders > 0:
                        builds = runs.setdefault((buildNumber, rev), {})
                    else:
                        builds = runs.setdefault(rev, {})
                        # pick the most recent or ?

                    if builderName not in builds:
                        builds[builderName] = build.getNumber()

        for cat_branch, (runs, no_revision_builds) in cat_branches.items():
            self._prune_runs(runs, N)
            for label, runBuilds in runs.iteritems():
                for builderName, buildNumber in runBuilds.items():
                    key = (builderName, buildNumber)
                    outcome_set = outcome_set_cache.get(status, key)
                    runBuilds[builderName] = outcome_set

        return cat_branches

    @staticmethod
    def _parse_builds(build_select):
        builds = set()
        for sel in build_select:
            for onesel in sel.split(','):
                build = safe_int(onesel)
                if build is not None:
                    builds.add(build)
                    continue
                build_start_end = onesel.split('-')
                if len(build_start_end) == 2:
                    build_start = safe_int(build_start_end[0])
                    build_end = safe_int(build_start_end[1])
                    if (build_start is not None and build_end is not None):
                        builds.update(range(build_start, build_end+1))
        return builds

    def _cat_branch_key(self, (category, branch)):
        branch_key = (0,)
        if branch is not None:
            for j, prefix in enumerate(self.branch_order_prefixes):
                if branch.startswith(prefix):
                    branch_key = (j+1, branch)
                    break
            else:
                branch_key = (len(self.branch_order_prefixes)+1, branch)
        i = self.PLATFORM_PRIORITY.get(category, len(self.categories))
        cat_key = (i, category)
        return cat_key + branch_key

    def body(self, request):
        t0 = time.time()
        outcome_set_cache.reset_counters()

        status = self.getStatus(request)

        page = SummaryPage(status)
        #page.sections.append(repr(request.args))

        only_branches = request.args.get('branch', None)
        only_recentrevs = request.args.get('recentrev', None)
        if only_branches is not None:
            branches = []
            for x in map(default_value, only_branches):
                if isinstance(x, str):
                    branches.append(x)
                else:
                    branches.extend(x)
            only_branches = branches
        only_builder = request.args.get('builder', None)
        only_builds = None
        if only_builder is not None:
            page.n_builders = len(only_builder)
            build_select = request.args.get('builds', None)
            if build_select is not None:
                only_builds = self._parse_builds(build_select)
        only_categories = request.args.get('category', None)
        if only_categories is not None:
            only_categories = map(nocat_value, only_categories)

        cat_branches = self.recentRuns(status,
                                   only_recentrevs = only_recentrevs,
                                   only_branches = only_branches,
                                   only_builder = only_builder,
                                   only_builds = only_builds,
                                   only_categories = only_categories
                                   )

        sorting = sorted(cat_branches.iterkeys(), key=self._cat_branch_key)
        for cat_branch in sorting:
            runs, no_revision_builds = cat_branches[cat_branch]
            outcome_sets = []
            for label, by_build in runs.items():
                outcome_sets.append(GatherOutcomeSet(by_build))
            page.add_section(cat_branch, outcome_sets)
            page.add_no_revision_builds(status, no_revision_builds)

        t1 = time.time()
        total_time = time.time()-t0
        page.add_comment('t=%.2f; %s' % (total_time,
                                         outcome_set_cache.stats()))

        if request.args:
            default_vs_any_text = "filter nothing"
            default_vs_any_query = ""
        else:
            default_vs_any_text = "all main"
            default_vs_any_query = "?branch=main"

        default_vs_any_anchor = html.a(default_vs_any_text,
                                     href="/summary%s" %
                                     default_vs_any_query,
                                     class_="failSummary trunkVsAny")
        default_vs_any = html.div(default_vs_any_anchor,
                                style="position: absolute; right: 5%;")
        return default_vs_any.unicode() + page.render()
