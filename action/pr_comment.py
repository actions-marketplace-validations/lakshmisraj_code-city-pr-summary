#!/usr/bin/env python3
"""Code City PR summary: comment on a pull request with the hotspots and owners of the files it touches.

Runs inside GitHub Actions (see action.yml). Needs the repo checked out with full history
(actions/checkout with fetch-depth: 0) and a token that can write pull request comments.
Teams can tune it with a .codecity.yml file in the repo (see README). Set DRY_RUN=1 to print
the comment instead of posting it.
"""
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

MARKER = "<!-- code-city-pr-summary -->"   # finds our earlier comment so pushes update it instead of adding more
MAX_ROWS = 15
DEFAULTS = {"ignore": [], "test-paths": [], "hotspot-top-percent": 10, "owner-hints": True, "test-check": True,
            "quiet": False, "history-days": 365}
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__|spec|specs|e2e|cypress|fixtures|testdata)/"   # test folders
    r"|(^|/)(test_[^/]+|tests?|conftest)\.py$"                                         # pytest
    r"|[^/]+_(test|spec)\.\w+$|[^/]+-test\.\w+$"                                       # foo_test.go, foo_spec.rb, foo-test.js
    r"|\.(test|spec)\.[^/]+$"                                                          # foo.test.ts
    r"|[A-Za-z]Tests?\.(java|kt|kts|cs|scala|swift|php)$",                             # FooTest.java, FooTests.cs
    re.I)
CODE_EXT = re.compile(r"\.(ts|tsx|mts|cts|js|jsx|mjs|cjs|vue|svelte|py|go|rs|java|kt|kts|scala|c|h|cc|hh|cpp|hpp|m|mm"
                      r"|cs|rb|php|swift|dart|ex|exs|lua)$", re.I)
NOT_CODE = re.compile(r"(^|/)(docs?|examples?)/|\.d\.ts$|\.stories\.\w+$|(^|/)[^/]*\.config\.\w+$|(^|/)\.[^/]+rc\.\w+$", re.I)


def git(*args):
    r = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                       cwd=os.environ.get("GITHUB_WORKSPACE", "."), errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


# ---- settings ----

def strip_comment(line):
    """Drop a `# comment`, but not a # inside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def split_flow(text):
    """`a, "b,c", d` -> ['a', '"b,c"', 'd']: split on commas outside quotes."""
    parts, cur, quote = [], "", None
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            parts.append(cur); cur = ""
            continue
        cur += ch
    parts.append(cur)
    return [p for p in parts if p.strip()]


def parse_value(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [parse_value(v) for v in split_flow(text[1:-1])]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def read_settings(text):
    """The small part of YAML .codecity.yml needs: `key: value`, `key: [a, b]`, and `key:` followed by `- item` lines.
    Reading it ourselves means the action needs nothing installed beyond Python."""
    settings, key, warnings = dict(DEFAULTS), None, []
    for raw in text.lstrip("﻿").splitlines():
        line = strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        item = re.match(r"^\s*-\s*(.+)$", line)
        if item and key is not None:
            settings[key].append(parse_value(item.group(1)))
            continue
        pair = re.match(r"""^(["']?)([A-Za-z][\w-]*)\1\s*:\s*(.*)$""", line)
        if not pair or line[0].isspace():
            warnings.append(f"couldn't read line (only top-level settings and simple lists are supported): {raw.strip()}")
            continue
        name, value = pair.group(2), pair.group(3)
        if name not in DEFAULTS:
            warnings.append(f"unknown setting `{name}`")
            key = None
            continue
        if value == "":
            settings[name], key = [], name
        else:
            settings[name], key = parse_value(value), None
    # Wrong types fall back to the default rather than breaking the summary.
    for name, default in DEFAULTS.items():
        v = settings[name]
        if isinstance(default, bool) and not isinstance(v, bool):
            warnings.append(f"`{name}` should be true or false"); settings[name] = default
        elif isinstance(default, int) and not isinstance(default, bool) and (isinstance(v, bool) or not isinstance(v, int) or v <= 0):
            warnings.append(f"`{name}` should be a positive number"); settings[name] = default
        elif isinstance(default, list) and not isinstance(v, list):
            settings[name] = [v]
    for name in ("ignore", "test-paths"):
        settings[name] = [str(p) for p in settings[name] if str(p).strip()]
    settings["hotspot-top-percent"] = min(settings["hotspot-top-percent"], 100)
    return settings, warnings


def glob_regex(pattern):
    """Gitignore-style patterns: `*` stays within a folder, `**` spans folders, a trailing `/` means a folder,
    and a pattern without a `/` (other than a trailing one) matches at any depth."""
    p, folder = pattern.strip(), pattern.strip().endswith("/")
    p = p.rstrip("/")
    anchored = "/" in p
    p = p.lstrip("/")
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif p.startswith("**", i):
            out += ".*"; i += 2
        elif p[i] == "*":
            out += "[^/]*"; i += 1
        elif p[i] == "?":
            out += "[^/]"; i += 1
        elif p[i] == "[" and "]" in p[i + 1:]:
            j = p.index("]", i + 1)
            body = p[i + 1:j].replace("\\", "\\\\")
            if body.startswith("!"):   # only a leading ! negates a class, as in .gitignore
                body = "^" + body[1:]
            out += "[" + body + "]"; i = j + 1
        else:
            out += re.escape(p[i]); i += 1
    # A folder pattern matches everything inside it; any other pattern also matches a folder of that name.
    tail = "/.*" if folder else "(?:/.*)?"
    return re.compile(("^" if anchored else "^(?:.*/)?") + out + tail + "$")


def ignored(path, patterns):
    return any(glob_regex(p).match(path) for p in patterns)


# ---- git facts ----

def changed_files(base, head):
    """[(status, path, old_path)] for the PR, measured from where it branched off."""
    out, parts = [], git("diff", "-z", "--name-status", "-M", f"{base}...{head}").split("\0")
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i][:1]
        if status in "RC":
            out.append(("R" if status == "R" else "A", parts[i + 2], parts[i + 1] if status == "R" else None)); i += 3
        else:
            out.append((status, parts[i + 1], None)); i += 2
    return out


def history(ref, days, patterns):
    """Per file: how many commits touched it, and who made them, over the last `days` days.
    Also each author name's email addresses, so the PR's own author can be recognised under another name."""
    churn, authors, emails = {}, {}, {}
    author = None
    for tok in git("log", f"--since={days} days ago", "--no-merges", "-z", "--format=%x01%aN%x02%aE", "--name-only", ref).split("\0"):
        tok = tok.lstrip("\n")
        if tok.startswith("\x01"):
            author, _, email = tok[1:].partition("\x02")
            author = author.strip()
            emails.setdefault(author, set()).add(email.strip().lower())
        elif tok and not ignored(tok, patterns):
            churn[tok] = churn.get(tok, 0) + 1
            authors.setdefault(tok, {})
            authors[tok][author] = authors[tok].get(author, 0) + 1
    return churn, authors, emails


def noreply_login(email):
    """12345+octocat@users.noreply.github.com -> octocat"""
    m = re.match(r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", email)
    return m.group(1).lower() if m else None


def pr_authors(base, head, logins):
    """Names, emails and logins of everyone who wrote commits in the PR, plus whoever opened it and owns its branch,
    so they are never suggested as its reviewer."""
    who = {l.lower() for l in logins if l}
    for line in git("log", "--format=%aN%x02%aE", f"{base}..{head}").splitlines():
        name, _, email = line.partition("\x02")
        who |= {name.strip().lower(), email.strip().lower()}
        if noreply_login(email.strip().lower()):
            who.add(noreply_login(email.strip().lower()))
    return who - {""}


def is_bot(name):
    return bool(re.search(r"\[bot\]$|^(dependabot|renovate|github-actions|greenkeeper|snyk-bot)\b", name, re.I))


def repo_has_tests(ref, extra):
    return any(is_test(p, extra) for p in git("ls-tree", "-r", "--name-only", ref).splitlines())


def is_test(path, extra):
    return bool(TEST_PATH.search(path)) or ignored(path, extra)


def is_code(path, extra):
    return bool(CODE_EXT.search(path)) and not NOT_CODE.search(path) and not is_test(path, extra)


def hot_threshold(churn, top_percent):
    """A file is a hotspot when its change count is in the repo's top `top_percent`% (and at least 2 changes)."""
    counts = sorted(churn.values(), reverse=True)
    if not counts:
        return float("inf")
    return max(2, counts[max(0, math.ceil(len(counts) * top_percent / 100) - 1)])


# ---- the comment ----

def code(text, in_table=False):
    """A file path as inline code that survives backticks in the name, and pipes when it sits in a table."""
    run = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (run + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    if in_table:
        text = text.replace("|", "\\|")
    return fence + pad + text + pad + fence


def plain(text):
    """A git author name as plain text: no markdown, no table breaks, and no @mentions that would ping someone."""
    text = re.sub(r"([\\`*_\[\]<>|~#])", r"\\\1", text)
    return text.replace("@", "@​")


def build_comment(repo, number, files, churn, authors, settings, site, exclude=frozenset(), has_tests=False, skipped=0,
                  emails=None, all_files=None):
    """Returns (comment, worth_saying). Worth saying: a hotspot is touched, tests look missing, or there's a reviewer hint."""
    days, pct = settings["history-days"], settings["hotspot-top-percent"]
    emails = emails or {}
    hot_at = hot_threshold(churn, pct)
    rows = []
    for status, path, old in files:
        known_as = old or path   # a renamed file's history is under its old name
        c = churn.get(known_as, 0)
        who = {a: n for a, n in authors.get(known_as, {}).items() if not is_bot(a)}
        top = max(who.items(), key=lambda kv: kv[1])[0] if who else None
        rows.append({"status": status, "path": path, "old": old, "churn": c, "hot": c >= hot_at,
                     "owner": top, "solo": len(who) == 1 and c >= 2})
    rows.sort(key=lambda r: (not r["hot"], -r["churn"], r["path"]))

    def is_pr_author(name):
        mails = emails.get(name, set())
        return name.lower() in exclude or bool(mails & exclude) or any(noreply_login(m) in exclude for m in mails)

    hot = [r for r in rows if r["hot"]]
    # Reviewer hints: a file only one person has changed, when that person isn't writing this PR. Grouped by person.
    experts = {}
    if settings["owner-hints"]:
        for r in rows:
            if r["solo"] and r["owner"] and not is_pr_author(r["owner"]):
                experts.setdefault(r["owner"], []).append(r["path"])
    # Missing tests: code changed, no test file did, in a repo that has tests at all. Counted over every changed
    # file, ignored ones included, so a test change in an ignored folder still counts.
    extra = settings["test-paths"]
    every = all_files if all_files is not None else files
    code_changed = any(is_code(p, extra) and s != "D" for s, p, _ in every)
    tests_changed = any(is_test(p, extra) for _, p, _ in every)
    tests_missing = settings["test-check"] and has_tests and code_changed and not tests_changed

    label = {"A": "new", "D": "deleted", "R": "renamed", "M": "changed"}
    period = "last year" if days == 365 else f"last {days} days"
    title = f"### 🏙 Code City: this PR touches {len(rows)} file{'s' if len(rows) != 1 else ''}"
    lines = [MARKER, title + (f" ({skipped} ignored by .codecity.yml)" if skipped else ""), ""]
    if hot:
        lines.append(f"⚠️ **{len(hot)} hotspot{'s' if len(hot) != 1 else ''}**: files in the repo's top {pct}% for how often they change. Bugs tend to cluster in them, so review these closely.")
    else:
        lines.append("✅ No hotspots: none of these files is among the repo's most often changed.")
    if tests_missing:
        lines.append("🧪 Code changed but no test files did. Consider adding or updating tests.")
    for owner, paths in list(experts.items())[:3]:
        shown = ", ".join(code(p) for p in paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
        lines.append(f"👤 **{plain(owner)}** is the only person who has changed {shown} in the {period}. They may be the best reviewer.")
    lines += ["", f"| | File | Commits ({period}) | Changed most by |", "|---|---|---|---|"]
    for r in rows[:MAX_ROWS]:
        flag = "🔥" if r["hot"] else ""
        name = code(r["path"], True) + (" (was " + code(r["old"], True) + ")" if r["old"] else "")
        lines.append(f"| {flag} {label.get(r['status'], 'changed')} | {name} | {r['churn']} | {plain(r['owner']) if r['owner'] else '–'} |")
    if len(rows) > MAX_ROWS:
        lines.append(f"| | …and {len(rows) - MAX_ROWS} more | | |")
    lines += ["", f"[**Open this PR in Code City →**]({site.rstrip('/')}/?repo={repo}&pr={number}&mode=heat)",
              "", "<sub>Counts come from git history; names are as they appear in commits. Tune this summary with a `.codecity.yml` file.</sub>"]
    return "\n".join(lines), bool(hot or tests_missing or experts)


QUIET_ALL_CLEAR = MARKER + "\n✅ **Code City:** nothing to flag after the latest push."


# ---- posting ----

def github(method, url, token, body=None):
    for attempt in range(3):   # GitHub's API has brief 5xx hiccups; try again before giving up
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "code-city-action"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def own_login(api, token):
    """The account our comments are posted as. The workflow's own token can't read /user (GitHub answers 403)
    and posts as github-actions[bot]; a personal token posts as its owner. Any other failure is raised, so a
    network blip can't make us miss our earlier comment and post a second one."""
    try:
        return github("GET", f"{api}/user", token)["login"]
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "github-actions[bot]"
        raise


def post(repo, number, body, token, only_update=False):
    """Post the comment, or update our earlier one. With only_update, never start a new comment."""
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    me = own_login(api, token)
    mine = None
    for page in range(1, 51):   # every page of comments, so a long discussion can't hide our earlier one
        comments = github("GET", f"{api}/repos/{repo}/issues/{number}/comments?per_page=100&page={page}", token)
        # Only our own account's comment counts: anyone can paste the marker into a comment of theirs.
        mine = next((c for c in comments if (c.get("user") or {}).get("login") == me and (c.get("body") or "").startswith(MARKER)), None)
        if mine or len(comments) < 100:
            break
    if mine:
        github("PATCH", f"{api}/repos/{repo}/issues/comments/{mine['id']}", token, {"body": body})
        print(f"Updated Code City comment on #{number}")
    elif only_update:
        print("Code City: nothing worth flagging, so no comment (quiet mode).")
    else:
        github("POST", f"{api}/repos/{repo}/issues/{number}/comments", token, {"body": body})
        print(f"Posted Code City comment on #{number}")


def positive_int(text, name, fallback):
    try:
        n = int(str(text).strip())
        if n > 0:
            return n
    except ValueError:
        pass
    print(f"::warning::Code City: `{name}` should be a positive number, not {text!r}. Using {fallback}.")
    return fallback


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event.get("pull_request")
    if not pr:
        print("Code City: not a pull request event, nothing to do.")
        return
    repo, number = os.environ["GITHUB_REPOSITORY"], pr["number"]
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    site = os.environ.get("CODE_CITY_SITE") or "https://code-city-view.netlify.app"
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    from_fork = bool(head_repo) and head_repo != repo

    # Settings come from the base branch, so a PR can't quietly switch off its own checks.
    config = os.environ.get("CODE_CITY_CONFIG") or ".codecity.yml"
    try:
        text = git("show", f"{base}:{config}")
    except RuntimeError:
        text = ""
    settings, warnings = read_settings(text)
    for w in warnings:
        print(f"::warning file={config}::{w}")
    # Action inputs, when given, win over the file.
    if os.environ.get("CODE_CITY_DAYS", "").strip():
        settings["history-days"] = positive_int(os.environ["CODE_CITY_DAYS"], "history-days", settings["history-days"])
    if os.environ.get("CODE_CITY_QUIET", "").strip():
        settings["quiet"] = os.environ["CODE_CITY_QUIET"].strip().lower() in ("true", "yes", "1", "on")

    try:
        shallow = git("rev-parse", "--is-shallow-repository").strip() == "true"
    except RuntimeError:
        shallow = False
    try:
        all_files = changed_files(base, head)
    except RuntimeError as e:
        hint = "Add `fetch-depth: 0` to actions/checkout so the full history is there." if shallow else \
               "The PR's base and head commits must both be in the checkout (with pull_request_target, fetch the PR head first)."
        print(f"::warning::Code City couldn't compare this PR with its base. {hint} ({e})")
        return
    if shallow:
        print("::warning::Code City: the checkout is shallow, so hotspots and owners only see part of the history. Add `fetch-depth: 0` to actions/checkout.")
    files = [f for f in all_files if not ignored(f[1], settings["ignore"])]
    def clear_earlier(reason):   # a later push left nothing to look at: don't leave old warnings up
        print(f"Code City: {reason}")
        if not os.environ.get("DRY_RUN"):
            try:
                post(repo, number, QUIET_ALL_CLEAR, os.environ["GITHUB_TOKEN"], only_update=True)
            except urllib.error.HTTPError as e:
                print(f"::warning::Code City couldn't update its earlier comment (HTTP {e.code}).")
    if not files:
        return clear_earlier("this PR changes no files it looks at.")
    churn, authors, emails = history(base, settings["history-days"], settings["ignore"])
    logins = [(pr.get("user") or {}).get("login"), ((pr.get("head") or {}).get("user") or {}).get("login")]
    body, worth = build_comment(repo, number, files, churn, authors, settings, site,
                                pr_authors(base, head, logins), repo_has_tests(base, settings["test-paths"]),
                                len(all_files) - len(files), emails, all_files)
    quiet_skip = settings["quiet"] and not worth
    if quiet_skip:
        body = QUIET_ALL_CLEAR
    if os.environ.get("DRY_RUN"):
        print(("[quiet: only updates an earlier comment]\n" if quiet_skip else "") + body)
        return
    try:
        post(repo, number, body, os.environ["GITHUB_TOKEN"], only_update=quiet_skip)
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            reason = e.read().decode("utf-8", "replace")[:200] if hasattr(e, "read") else ""
            if from_fork:
                print(f"::warning::Code City couldn't comment (HTTP {e.code}): pull requests from forks get a read-only token, so the summary is skipped for them.")
            elif "rate limit" in reason.lower():
                print(f"::warning::Code City couldn't comment: GitHub's API rate limit was reached. It will try again on the next push.")
            else:
                print(f"::warning::Code City couldn't comment (HTTP {e.code}). Make sure the workflow has `permissions: pull-requests: write`. GitHub said: {reason}")
            return
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # never fail someone's CI because of a summary comment
        print(f"::warning::Code City summary skipped: {e}")
        sys.exit(0)
