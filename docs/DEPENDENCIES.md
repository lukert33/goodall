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

None.

## Adopted: development and build tools

None of these ships to people who install goodall.

| Tool | Purpose | Provenance (checked 2026-11-16) | Decision |
|---|---|---|---|
| **uv** | Python, virtual environment and lockfile manager | Astral Software Inc.; `astral-sh/uv`; MIT OR Apache-2.0; self-contained Rust binary. Eight advisories since 2025-08, all in the installer and archive extraction, each fixed in a release (example: GHSA-4gg8-gxpx-9rph, arbitrary file write, 2026-05-18). | **adopt** |
| **uv_build** | Build backend (turns the source tree into an installable package) | Astral; ships inside the `uv` executable, so a compatible version downloads nothing extra; zero dependencies; MIT OR Apache-2.0. No advisory is attributed to the build backend. Pure-Python projects only, which is what goodall is. | **adopt** (owner approved 2026-11-16) |
| **pytest** 9.2.0 | Test runner | `pytest-dev`, a volunteer group with no single corporate owner; ~14,700 stars; MIT; four direct dependencies (`iniconfig`, `packaging`, `pluggy`, `pygments`). One advisory: CVE-2025-71176 (temporary-directory handling, medium), public 2026-01-22, fixed in 9.0.3 on 2026-04-07. The floor in `pyproject.toml` is 9.0.3 for that reason. | **adopt** (owner approved 2026-11-16) |
| **ruff** 0.17.5 | Linter and formatter | Astral; `astral-sh/ruff`, ~51,200 stars; MIT; zero dependencies, bundled Rust binary. No published GitHub advisories and no OSV results. | **adopt** (owner approved 2026-11-16) |
| **mypy** 2.4.1 | Type checker | `python/mypy`, original author Jukka Lehtosalo; ~20,700 stars; MIT; five direct dependencies (`typing_extensions`, `mypy_extensions`, `pathspec`, `librt`, `ast-serialize`). No GitHub advisories. | **adopt** (owner approved 2026-11-16) |

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
