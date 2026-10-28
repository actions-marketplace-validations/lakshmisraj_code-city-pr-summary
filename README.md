# Code City PR summary

A GitHub Action that comments on each pull request with what reviewers should know about the files it touches:

- **Hotspots:** files that change far more often than the rest of the repo. Bugs tend to cluster in them.
- **Missing tests:** code changed but no test files did (only in repos that have tests).
- **Who to ask:** files that only one other person has changed recently. It never suggests the PR's own author.
- **A link to see the PR in [Code City](https://code-city-lakshmi.netlify.app)**, a 3D view of the codebase with the PR's files lit up.

It runs in your repo's own GitHub Actions, so your code never leaves GitHub. It posts one comment and updates it on later pushes, and it never fails a build.

## Set it up

Add `.github/workflows/code-city.yml` to your repo:

```yaml
name: Code City PR summary
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  summary:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0   # full history is needed for hotspots and owners
      - uses: lakshmisraj/code-city-pr-summary@v1
```

Pull requests from forks get a read-only token, so they are skipped with a note in the log.

## What the comment looks like

> ### 🏙 Code City: this PR touches 3 files
> ⚠️ **2 hotspots**: files changed more often than 90% of the repo's files. Bugs tend to cluster in them, so review these closely.
> 🧪 Code changed but no test files did. Consider adding or updating tests.
> 👤 **Maya** is the only person who has changed `src/pay.ts` in the last year. They may be the best reviewer.
>
> | | File | Commits (last year) | Changed most by |
> |---|---|---|---|
> | 🔥 changed | `src/pay.ts` | 31 | Maya |
> | 🔥 changed | `src/cart.ts` | 24 | Sam |
> | changed | `README.md` | 3 | Lee |
>
> **Open this PR in Code City →**

## Settings: `.codecity.yml`

Put a `.codecity.yml` in your repo root to tune the summary. It is read from the base branch, so a pull request can't switch off its own checks. Every setting is optional.

```yaml
ignore:                   # folders (ending in /) or file patterns left out of the summary
  - vendor/
  - dist/
  - "**/*.generated.ts"
hotspot-top-percent: 10   # a hotspot is a file in the top 10% for changes
owner-hints: true         # suggest a reviewer when only one other person has changed a file
test-check: true          # flag code changes with no test changes
quiet: false              # true: only comment when something is worth flagging
history-days: 365         # how much history counts
```

**Quiet mode** comments only when a hotspot is touched, tests look missing, or there's a reviewer hint. If a later push clears everything, it updates its earlier comment to "nothing to flag" instead of leaving old warnings.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `quiet` | from `.codecity.yml` | `true` to comment only when something is worth flagging |
| `history-days` | from `.codecity.yml` (365) | How many days of history count towards hotspots and owners |
| `config` | `.codecity.yml` | Path of the settings file |
| `site` | `https://code-city-lakshmi.netlify.app` | Code City site the comment links to |
| `github-token` | the workflow's token | Token used to post the comment |
