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
