# Dependency provenance and decisions

Goodall runs on machines that hold live credentials for a research project. A
compromised dependency is a direct path to those credentials, so every package
earns its place here before it is added. This file records what was adopted,
what was rejected, and why.

The facts below were checked live on **2026-11-16** against the PyPI JSON API,
each project's GitHub repository and security advisories, and OSV.dev. Check
again before adding or upgrading anything.

## Standing rules

1. **Standard library first.** If the standard library does the job acceptably,
   use it. An exception needs a stated reason in the table below.
2. **Few direct dependencies.** Goodall has **zero** runtime dependencies today.
3. **Checked facts, not recommendations.** A package is added only after its
   maintainer, repository, advisory history, licence and dependency count have
   been looked up and written here. A suggestion from a person or a model is a
   lead to check.
4. **Single-maintainer packages are a first-class risk.** They can be adopted
   only with a stated justification, and never when a comparable
   multi-maintainer or standard-library option exists.
5. **The project owner approves every addition.** The request goes to the
   owner with the facts and with anything that could not be verified.
6. **The lockfile is committed.** `uv.lock` pins exact versions and hashes.
   Install with `uv sync`.
7. **The capture path stays lean.** Code run by Claude Code hooks imports only
   the standard library (AGENTS.md invariant 7).

## Adopted: runtime

None. Once goodall is installed, running it needs only Python 3.12 or newer
and the standard library.

## Needed to install

Goodall has no runtime dependencies, but something still has to put it on the
machine together with a Python new enough to run it. The Python that comes
with macOS's developer tools is older than 3.12 (3.9.6 on the machine this
was checked on, 2026-11-17), so most researchers' Macs will need both.

Which installer to use is up to the preferences or policy of whoever installs
goodall: the person or organisation that owns the Mac. An eval account is a
standard macOS user account and cannot install software itself. Goodall's own
instructions use uv, and that is the only route tested.

| Tool | Purpose | Provenance (checked 2026-11-16) | Decision |
|---|---|---|---|
| **uv** | Fetches a suitable Python and installs goodall as a command. Also the Python, virtual environment and lockfile manager used to develop goodall. | Astral Software Inc.; `astral-sh/uv`; MIT OR Apache-2.0; self-contained Rust binary. Eight advisories since 2025-08, all in the installer and archive extraction, each fixed in a release (example: GHSA-4gg8-gxpx-9rph, arbitrary file write, 2026-05-18). Every uv below 0.13.4 is affected by GHSA-hq85-5cpj-mr62 (high, 2026-10-20: a compromised certificate authority in uv's bundled list), so install 0.13.4 or newer. | **adopt** as the documented route |

Popular alternatives. None has been tested with goodall, and none has been
through the provenance check this file requires, so each is the installing
party's own decision:

- **pipx**, with a Python 3.12 or newer installed separately. pipx installs a
  Python command-line tool into its own environment, as `uv tool install`
  does, but does not fetch Python.
- **pip in a virtual environment** (`python -m venv`), again with a Python
  3.12 or newer installed separately. Nothing beyond Python itself is added.
- **A Python from python.org, Homebrew or an organisation's managed software
  catalogue**, to supply the interpreter for either of the two routes above.

Goodall is not published to a package index yet, so today every route
installs from a copy of this repository and builds it with `uv_build` (below),
which pip and pipx fetch for themselves.

## Programs goodall runs or works alongside

These are separate programs, installed by whoever owns the Mac. None is a
Python package inside goodall, so goodall's own runtime dependency count stays
at zero. They are listed because each one runs on the same machine, with the
same account's access, as the eval.

Checked live on **2026-11-17** against each project's GitHub repository and
security advisories, the PyPI JSON API, OSV.dev and 1Password's release notes.

| Program | Needed when | Provenance (checked 2026-11-17) | Decision |
|---|---|---|---|
| **git** | Always. Goodall commits and pushes records with it (ADR 0001). | The Git project, maintained by Junio C Hamano with a large contributor base; `git/git` is a mirror of the mailing-list repository, ~64,400 stars; GPL-2.0 (from the project's own licence file, not re-read today; GitHub's detector reports none). Newest release v2.56.1. 33 published advisories, most fixed promptly. One is open: GHSA-vp2w-7xxj-jvxr (high, 2026-11-10), code execution when cloning an attacker's repository with `--recurse-submodules`, no patch yet. Goodall never clones, so it does not take that path; a researcher cloning a record repo by hand should not use that option on a repository they do not trust. | **adopt** |
| **Claude Code** | Always. It is the agent tool goodall records (ADR 0002). | Anthropic; `anthropics/claude-code`, ~156,000 stars; no open-source licence in the repository, so treat it as proprietary. Newest release v2.1.326 (2026-11-16), with releases most days. 36 published advisories between 2025-06 and 2026-10 (29 high, 4 medium, 3 low), covering sandbox escapes, writes outside the workspace and plugin pinning; each names a fixed release. Goodall depends on its hook input and transcript layout, which Anthropic does not promise to keep stable. | **adopt**: it is the subject of the eval, so there is no alternative to weigh. Keep it updated. |
| **1Password's command-line tool, `op`** | Only when the env file holds a secret reference (ADR 0006). | 1Password (AgileBits Inc.); closed source, proprietary, so the code cannot be reviewed. Newest stable release 2.42.0 (2026-11-03). Known issues: CVE-2022-32550 (medium, fixed in 2.3.0); CVE-2024-42219 (2024-08, a local process on macOS could impersonate a trusted 1Password integration such as the command-line tool; fixed in the 1Password app 8.10.36). An independent researcher's 2023 disclosure about `op` on shared machines was not given a CVE or a fix. Goodall runs only `op whoami`, `op vault list` and `op item list`, and never a command that returns a secret value. | **adopt**, conditional. "Or an equivalent secrets manager" remains the intent; `op` is the only one supported. |
| **GitHub's command-line tool, `gh`** | Always: `goodall doctor` requires it. No goodall code runs it yet. | GitHub; `cli/cli`, ~47,100 stars; MIT. Newest release v2.104.0 (2026-11-12). 17 published advisories, two high (GHSA-8xvp-7hj6-mcj9, 2026-05; GHSA-p2h2-3vg9-4p87, 2024-11), the rest low or medium, each fixed in a release. It holds a GitHub token with whatever access the account granted it. | **adopt** (owner approved 2026-11-17, on the basis that GitHub publishes it) |

The owner approved the git, Claude Code and `op` rows on 2026-11-17 as well.

Versions on the machine goodall is developed on, 2026-11-17: git 2.55.0, `gh`
2.101.0, `op` 2.34.1. All three are behind the newest release.

### Approved, not yet used

No goodall code uses these yet. They are approved so the builds that need them
are not held up. Each still gets its row in `uv.lock`, or its install
instructions, in the change that first uses it.

| Program or package | What it would give | Provenance (checked 2026-11-17) | Decision |
|---|---|---|---|
| **inspect-ai** 0.3.290 | The transcript and event format goodall exports to (AGENTS.md invariant 8: an optional extra, imported only by export code) | UK AI Security Institute; `UKGovernmentBEIS/inspect_ai`, ~3,300 stars; MIT; Python 3.11 or newer. 40 direct dependencies; installing it alone resolves to 80 packages. No GitHub advisories and no OSV results. Releases several times a week, and the version number is still below 1.0. | **adopt** as an optional extra (owner approved 2026-11-17, on the basis that it comes from the UK AI Security Institute's ecosystem) |
| **inspect-scout** 0.6.2 | Analysis of agent transcripts; the target of the planned `goodall export scout` | Meridian Labs; `meridianlabs-ai/inspect_scout`, 90 stars, created 2025-09; MIT. Most commits come from two people who are also core Inspect maintainers. 30 direct dependencies including `inspect-ai`, `duckdb`, `pandas` and `pyarrow`; installed together with `inspect-ai` the total is 95 packag| **ActivityWatch** v0.14.3 | A record of which application and window had focus, as corroboration of what the human did. Optional for an eval. | `ActivityWatch/activitywatch`, ~19,700 stars; MPL-2.0. Two advisories, both high and both old: a DNS rebinding attack on its local server (2022) and shell execution from a website on macOS (2021). It runs a local web server and records window titles, which can contain private text. See "ActivityWatch: the highest-risk recommendation" below. | **adopt** as an optional, recommended program (owner approved 2026-11-17, knowing it is largely one maintainer's project) |ndow titles, which can contain private text. | **not yet assessed**: decide when the integration is designed. The single-maintainer rule (standing rule 4) applies. |

The 95 packages would be installed only by someone who asks for the export
extra. A researcher who only records an eval would not get them.

### ActivityWatch: the highest-risk recommendation

Of everything goodall recommends, ActivityWatch carries the most risk: it is
largely one person's project, it runs a local web server, and it records
window titles. Standing rule 4 asks for a stated justification before a
single-maintainer project is adopted. The owner's justification is the
project's age and the activity around it, set out here as checked on
2026-11-17.

- **Life of the project.** The repository was created on 2016-04-27 and its
  first release, v0.1, followed on 2016-06-29. It has 77 releases over ten
  years. Erik Bjäreholt founded it and still leads it; Johan Bjäreholt is the
  second long-term author, mostly on the Rust server (266 commits there to
  Erik's 386).
- **Size of the community.** ~19,700 stars, 1,078 forks, 37 contributors to
  the main repository, and 61 repositories in the organisation (server,
  web interface, watchers for windows, input and browsers, an Android app).
  Twelve of those repositories were pushed to in the three days before the
  check. It takes donations through GitHub Sponsors, Open Collective, Patreon
  and Liberapay.
- **Activity over time, main repository commits per year.** 2021: 78.
  2022: 131. 2023: 107. 2024: 35. 2025: 10. 2026: 195 to date.
- **The quiet stretch.** No stable release appeared between v0.13.2
  (2024-10-05) and v0.14.0 (2026-10-06), two years, and 2025 saw ten commits.
  Four releases have followed since 2026-10-06.
- **Who is doing the recent work.** Of the 191 commits in 2026 that could be
  attributed, 80 came from `TimeToBuildBob`, an account whose profile says it
  is an AI agent working with Erik Bjäreholt; 66 from Erik Bjäreholt; 36 from
  an automated dependency updater; and 9 from five other people. Of the last
  100 merged pull requests (2026-08-20 to 2026-11-15), 40 were the agent's,
  28 Erik Bjäreholt's, 8 the updater's and 8 from six other people.

Read together: a ten-year-old project with a large user base and a founder
who is still shipping, whose current burst of activity is mostly the founder
and his AI agent. If Erik Bjäreholt stepped away, it is not clear who would
publish security fixes. That is the risk being accepted. Goodall keeps it
optional so that an eval can run without it, and revisits this row if a year
passes without a release.

## Adopted: development and build tools

None of these is needed to install or run goodall, with the one exception
noted for `uv_build`. uv, listed above, is also the tool goodall is developed
with.

| Tool | Purpose | Provenance (checked 2026-11-16) | Decision |
|---|---|---|---|
| **uv_build** | Build backend (turns the source tree into an installable package) | Astral; ships inside the `uv` executable, so a compatible version downloads nothing extra; zero dependencies; MIT OR Apache-2.0. No advisory is attributed to the build backend. Pure-Python projects only, which is what goodall is. Until goodall is published as a ready-built package, installing it from this repository runs this backend on the installing machine. | **adopt** (owner approved 2026-11-16) |
| **pytest** 9.2.0 | Test runner | `pytest-dev`, a volunteer group with no single corporate owner; ~14,700 stars; MIT; four direct dependencies (`iniconfig`, `packaging`, `pluggy`, `pygments`). One advisory: CVE-2025-71176 (temporary-directory handling, medium), public 2026-01-22, fixed in 9.0.3 on 2026-04-07. The floor in `pyproject.toml` is 9.0.3 for that reason. | **adopt** (owner approved 2026-11-16) |
| **ruff** 0.17.5 | Linter and formatter | Astral; `astral-sh/ruff`, ~51,200 stars; MIT; zero dependencies, bundled Rust binary. No published GitHub advisories and no OSV results. | **adopt** (owner approved 2026-11-16) |
| **mypy** 2.4.1 | Type checker | `python/mypy`, original author Jukka Lehtosalo; ~20,700 stars; MIT; five direct dependencies (`typing_extensions`, `mypy_extensions`, `pathspec`, `librt`, `ast-serialize`). No GitHub advisories. | **adopt** (owner approved 2026-11-16) |

## Adopted: continuous integration

What `.github/workflows/quality-gates.yml` relies on. None of it ships to
people who install goodall, but it runs this repository's code, so it is held
to the same standard.

| Component | Purpose | Provenance (checked 2026-11-16) | Decision |
|---|---|---|---|
| **GitHub-hosted runner** `ubuntu-24.04` | Machine the gates run on | GitHub; image built from `actions/runner-images` (MIT), latest release `ubuntu24/20261111.370` on 2026-11-11. Named by version rather than `ubuntu-latest`, so the image line changes only when this file and the workflow do. Goodall targets macOS; the tests need only git, sh and Python, so Linux is used. | **adopt** |
| **actions/checkout** v7.0.1, commit `3d3c42e5aac5ba805825da76410c181273ba90b1` | Fetches the repository into the runner | GitHub; `actions/checkout`, ~9,000 stars; MIT. Released 2026-07-20; the tag is lightweight and points straight at that commit. No published security advisories. Pinned by commit id, with `persist-credentials: false` so the token is not left on disk. | **adopt** |
| **uv** 0.11.33, installed with pip | Runs the gates, as on a development machine | Same project as the uv row above. Installed into a throwaway virtual environment by `pip install --require-hashes`, against the sha256 of the PyPI wheel `uv-0.11.33-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` (`9542178978b0b6f16a7ae99e55aca039f493a1edb373a15d7993eab80a28615a`, uploaded 2026-07-28). 0.11.33 is the newest release inside the `uv_build` range in `pyproject.toml`. Every uv below 0.13.4 is affected by GHSA-hq85-5cpj-mr62 (high, 2026-10-20: a compromised certificate authority in uv's bundled list); the workflow sets `UV_NATIVE_TLS` so uv trusts the runner's system store instead. | **adopt** |
| **astral-sh/setup-uv** | Third-party action that installs uv | — | **rejected**: one more repository with write access to what CI runs; a pinned, hash-checked `pip install` does the same job. |

### Why pytest, when the standard library has `unittest`

`unittest` would run this project's tests, and rule 1 points at it. The owner
chose pytest anyway, for reasons specific to this project:

- Goodall intends to contribute its transcript parsing to Inspect, and Inspect
  and Inspect Scout test with pytest. Code and fixture tests meant for upstream
  have to be written in that style regardless.
- The project works test-first and outside-in (AGENTS.md, "Testing"). Plain
  `assert`, fixtures such as `tmp_path`, and parametrised cases keep that loop
  short to write and easy to read for researchers.
- It is the most widely used Python test style, so more contributors already
  know it.

The cost is four small development-only packages.

### One owner behind three tools

`uv`, `uv_build` and `ruff` all come from Astral. OpenAI announced on
2026-03-19 that it will acquire Astral; whether that has closed could not be
confirmed. Three tools therefore share one owner whose future is unsettled.
They were still chosen because each alternative is weaker on the measures this
file cares about (see the rejections). Revisit if Astral's licensing,
governance or release practice changes.

## Rejected

| Package | What it would give | Why rejected |
|---|---|---|
| **hatchling** 1.33.0 | Build backend | Five direct dependencies and effectively one maintainer (Ofek Lev). `uv_build` does the same job with no dependencies and no new maintainer to trust. |
| **setuptools** 84.2.1 | Build backend | Several advisories since 2024 (path traversal, command injection) and more dependencies than `uv_build`. |
| **ty** 0.0.104 | Type checker | Its README calls it beta with no stable interface. High-severity advisory GHSA-vxvm-j4xq-q7m4 (2026-09-24): code execution from analysing a crafted project. Revisit when stable. |
| **pyright** (PyPI package) | Type checker | The PyPI package is a third-party wrapper around Microsoft's checker and downloads Node at run time when Node is absent. Unacceptable on a credential-holding machine. |
| **pyrefly** 1.4.1 | Type checker | A credible runner-up: zero dependencies, MIT, marked production-stable. Younger than mypy, and its move from Meta's organisation to `pyrefly-org` could not be fully traced. Revisit if mypy becomes a burden. |
| **black + flake8** | Formatter and linter | Two tools with several dependencies between them; `ruff` covers both with none. |
| **OpenTelemetry Collector** v0.165.0 | Receives Claude Code's telemetry and writes it to files | `open-telemetry/opentelemetry-collector`, ~7,800 stars; Apache-2.0; a CNCF project. Four advisories, two of them published 2026-11-12. Rejected in ADR 0002: a separately downloaded binary and its configuration would have to be installed and kept running in every eval account, where a small standard-library receiver inside goodall does the one job needed. Goodall's output stays convertible to the Collector's format. |

## Could not verify

Recorded so that "not stated" is never mistaken for "confirmed safe".

- Whether OpenAI's acquisition of Astral has closed.
- pytest's transitive dependency count beyond its four direct dependencies.
- When pytest's temporary-directory issue was first reported privately.
- Who currently employs mypy's core maintainers.
- OSV results for mypy (the lookup returned nothing usable).
- Whether ruff's binary builds are reproducible.
- Whether pyrefly is still controlled by Meta.
- Whether the `ubuntu-24.04` runner's system certificate store already
  distrusts the compromised emSign ECC Root CA - G3 intermediates
  (GHSA-hq85-5cpj-mr62). `UV_NATIVE_TLS` relies on it doing so.
- 1Password's own list of security advisories. The search returned news
  reports and vulnerability databases, not 1Password's advisory pages, so the
  `op` row may be incomplete.
- Git's licence and Claude Code's licence terms were not read today.
- Whether GHSA-vp2w-7xxj-jvxr in git has been patched since it was published.
- Who else maintains ActivityWatch day to day, beyond commit counts.
- The 80 and 95 package totals come from one resolution for Python 3.12 on
  2026-11-17 and will drift.
- Whether goodall installs and runs correctly through pipx or through pip in
  a virtual environment. Only uv has been used.
- Advisory GHSA-hq85-5cpj-mr62 itself: it was reported by the agent that
  wrote the CI workflow and has not been checked a second time.
- Whether the pip and Python that ship on the runner image are themselves
  current; they are used only to install uv, whose wheel is hash-checked.

## Adding or upgrading a dependency

1. Can the standard library or an existing dependency do it acceptably? If so,
   stop.
2. Identify the maintainer or organisation, the canonical repository and where
   it is hosted. One maintainer, or hosting that cannot be reviewed commit by
   commit, raises the bar.
3. Check the advisory history (OSV.dev, GitHub Security Advisories) and how
   quickly past issues were fixed.
4. Count direct and transitive dependencies. Each one is trust extended.
5. Write the request for the owner, including what you could not verify.
6. After approval: add the row here, then `uv add` (or `uv add --dev`), then
   commit the updated `uv.lock` in the same change.
7. Run the tests, the linter and the type checker before pushing.
