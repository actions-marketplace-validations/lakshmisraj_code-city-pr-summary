#!/usr/bin/env python3
"""Code City PR summary: comment on a pull request with the hotspots and owners of the files it touches.

Runs inside GitHub Actions (see action.yml). Needs the repo checked out with full history
(actions/checkout with fetch-depth: 0) and a token that can write pull request comments.
Teams can tune it with a .codecity.yml file in the repo (see README). Set DRY_RUN=1 to print
the comment instead of posting it.
"""
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

MARKER = "<!-- code-city-pr-summary -->"   # finds our earlier comment so pushes update it instead of adding more
MAX_ROWS = 15
DEFAULTS = {"ignore": [], "hotspot-top-percent": 10, "owner-hints": True, "test-check": True, "quiet": False, "history-days": 365}
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]+\.py$|[^/]+_test\.(go|py)$|\.(test|spec)\.[^/]+$", re.I)
CODE_EXT = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|vue|svelte|py|go|rs|java|kt|kts|scala|c|h|cc|cpp|hpp|cs|rb|php|swift|dart|ex|exs|lua|sh)$", re.I)


def git(*args):
    r = subprocess.run(["git", "-c", "core.quotepath=off", *args], capture_output=True, text=True,
                       cwd=os.environ.get("GITHUB_WORKSPACE", "."), errors="replace")
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


# ---- settings ----

def parse_value(text):
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return [parse_value(v) for v in text[1:-1].split(",") if v.strip()]
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
    for raw in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()   # drop comments
        if not line.strip():
            continue
        item = re.match(r"^\s*-\s*(.+)$", line)
        if item and key is not None:
            settings[key].append(parse_value(item.group(1)))
            continue
        pair = re.match(r"^([A-Za-z][\w-]*)\s*:\s*(.*)$", line)
        if not pair:
            warnings.append(f"couldn't read line: {raw.strip()}")
            continue
        name, value = pair.group(1), pair.group(2)
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
    settings["ignore"] = [str(p) for p in settings["ignore"]]
    settings["hotspot-top-percent"] = min(settings["hotspot-top-percent"], 100)
    return settings, warnings


def ignored(path, patterns):
    """`vendor/` leaves out a folder anywhere; anything else is a glob such as `*.lock` or `src/**/*.gen.ts`."""
    for p in patterns:
        if p.endswith("/"):
            if path.startswith(p) or f"/{p}" in f"/{path}":
                return True
        elif fnmatch.fnmatch(path, p) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], p) or fnmatch.fnmatch(path, p.replace("**/", "")):
            return True
    return False


# ---- git facts ----

def changed_files(base, head):
    """[(status, path, old_path)] for the PR, measured from where it branched off."""
    out = []
    for line in git("diff", "--name-status", "-M", f"{base}...{head}").splitlines():
        parts = line.split("\t")
        status = parts[0][:1]
        if status == "R" and len(parts) == 3:
            out.append(("R", parts[2], parts[1]))
        elif len(parts) >= 2:
            out.append((status, parts[1], None))
    return out


def history(ref, days, patterns):
    """Per file: how many commits touched it, and who made them, over the last `days` days."""
    churn, authors = {}, {}
    author = None
    for line in git("log", f"--since={days} days ago", "--no-merges", "--format=%x01%aN", "--name-only", ref).splitlines():
        if line.startswith("\x01"):
            author = line[1:].strip()
        elif line.strip() and not ignored(line, patterns):
            churn[line] = churn.get(line, 0) + 1
            authors.setdefault(line, {})
            authors[line][author] = authors[line].get(author, 0) + 1
    return churn, authors


def pr_authors(base, head, login):
    """Everyone who wrote commits in the PR, plus whoever opened it, so they are never suggested as its reviewer."""
    names = {n.strip().lower() for n in git("log", "--format=%aN", f"{base}..{head}").splitlines() if n.strip()}
    if login:
        names.add(login.lower())
    return names


def repo_has_tests(ref):
    return any(TEST_PATH.search(p) for p in git("ls-tree", "-r", "--name-only", ref).splitlines())


def hot_threshold(churn, top_percent):
    """A file is a hotspot when its change count is in the repo's top `top_percent`% (and at least 2 changes)."""
    counts = sorted(churn.values(), reverse=True)
    if not counts:
        return float("inf")
    return max(2, counts[max(0, math.ceil(len(counts) * top_percent / 100) - 1)])


# ---- the comment ----

def build_comment(repo, number, files, churn, authors, settings, site, exclude=frozenset(), has_tests=False, skipped=0):
    """Returns (comment, worth_saying). Worth saying: a hotspot is touched, tests look missing, or there's a reviewer hint."""
    days, pct = settings["history-days"], settings["hotspot-top-percent"]
    hot_at = hot_threshold(churn, pct)
    rows = []
    for status, path, old in files:
        known_as = old or path   # a renamed file's history is under its old name
        c = churn.get(known_as, 0)
        who = authors.get(known_as, {})
        top = max(who.items(), key=lambda kv: kv[1])[0] if who else None
        rows.append({"status": status, "path": path, "old": old, "churn": c, "hot": c >= hot_at,
                     "owner": top, "solo": len(who) == 1 and c >= 2})
    rows.sort(key=lambda r: (not r["hot"], -r["churn"], r["path"]))

    hot = [r for r in rows if r["hot"]]
    # Reviewer hints: a file only one person has changed, when that person isn't writing this PR. Grouped by person.
    experts = {}
    if settings["owner-hints"]:
        for r in rows:
            if r["solo"] and r["owner"] and r["owner"].lower() not in exclude:
                experts.setdefault(r["owner"], []).append(r["path"])
    # Missing tests: code changed, no test file did, in a repo that has tests at all.
    code_changed = any(CODE_EXT.search(r["path"]) and not TEST_PATH.search(r["path"]) and r["status"] != "D" for r in rows)
    tests_changed = any(TEST_PATH.search(r["path"]) for r in rows)
    tests_missing = settings["test-check"] and has_tests and code_changed and not tests_changed

    label = {"A": "new", "D": "deleted", "R": "renamed", "M": "changed"}
    period = "last year" if days == 365 else f"last {days} days"
    title = f"### 🏙 Code City: this PR touches {len(rows)} file{'s' if len(rows) != 1 else ''}"
    lines = [MARKER, title + (f" ({skipped} ignored by .codecity.yml)" if skipped else ""), ""]
    if hot:
        lines.append(f"⚠️ **{len(hot)} hotspot{'s' if len(hot) != 1 else ''}**: files changed more often than {100 - pct}% of the repo's files. Bugs tend to cluster in them, so review these closely.")
    else:
        lines.append("✅ No hotspots: none of these files is among the repo's most often changed.")
    if tests_missing:
        lines.append("🧪 Code changed but no test files did. Consider adding or updating tests.")
    for owner, paths in list(experts.items())[:3]:
        shown = ", ".join(f"`{p}`" for p in paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
        lines.append(f"👤 **{owner}** is the only person who has changed {shown} in the {period}. They may be the best reviewer.")
    lines += ["", f"| | File | Commits ({period}) | Changed most by |", "|---|---|---|---|"]
    for r in rows[:MAX_ROWS]:
        flag = "🔥" if r["hot"] else ""
        name = f"`{r['path']}`" + (f" (was `{r['old']}`)" if r["old"] else "")
        lines.append(f"| {flag} {label.get(r['status'], 'changed')} | {name} | {r['churn']} | {r['owner'] or '–'} |")
    if len(rows) > MAX_ROWS:
        lines.append(f"| | …and {len(rows) - MAX_ROWS} more | | |")
    lines += ["", f"[**Open this PR in Code City →**]({site.rstrip('/')}/?repo={repo}&pr={number}&mode=heat)",
              "", "<sub>Counts come from git history; names are as they appear in commits. Tune this summary with a `.codecity.yml` file.</sub>"]
    return "\n".join(lines), bool(hot or tests_missing or experts)


QUIET_ALL_CLEAR = MARKER + "\n✅ **Code City:** nothing to flag after the latest push."


# ---- posting ----

def github(method, url, token, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "code-city-action"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def own_login(api, token):
    """The account our comments are posted as. The workflow's own token can't read /user, and posts as
    github-actions[bot]; a personal token posts as its owner."""
    try:
        return github("GET", f"{api}/user", token)["login"]
    except Exception:
        return "github-actions[bot]"


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


def main():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    pr = event.get("pull_request")
    if not pr:
        print("Code City: not a pull request event, nothing to do.")
        return
    repo, number = os.environ["GITHUB_REPOSITORY"], pr["number"]
    base, head = pr["base"]["sha"], pr["head"]["sha"]
    site = os.environ.get("CODE_CITY_SITE") or "https://code-city-lakshmi.netlify.app"

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
    if os.environ.get("CODE_CITY_DAYS"):
        settings["history-days"] = int(os.environ["CODE_CITY_DAYS"])
    if os.environ.get("CODE_CITY_QUIET"):
        settings["quiet"] = os.environ["CODE_CITY_QUIET"].strip().lower() in ("true", "yes", "1", "on")

    try:
        all_files = changed_files(base, head)
    except RuntimeError as e:
        print(f"::warning::Code City needs the full git history. Add `fetch-depth: 0` to actions/checkout. ({e})")
        return
    files = [f for f in all_files if not ignored(f[1], settings["ignore"])]
    if not files:
        print("Code City: this PR changes no files it looks at.")
        return
    churn, authors = history(base, settings["history-days"], settings["ignore"])
    body, worth = build_comment(repo, number, files, churn, authors, settings, site,
                                pr_authors(base, head, (pr.get("user") or {}).get("login")),
                                repo_has_tests(base), len(all_files) - len(files))
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
            # Pull requests from forks get a read-only token, so they can't be commented on.
            print(f"::warning::Code City couldn't comment on this PR (HTTP {e.code}). PRs from forks can't be commented on with the default token; make sure the workflow has `pull-requests: write`.")
            return
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # never fail someone's CI because of a summary comment
        print(f"::warning::Code City summary skipped: {e}")
        sys.exit(0)
