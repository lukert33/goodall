# Terminal library provenance

Research for beads ticket `goodall-xes.5`. The onboarding wizard
(`goodall onboard`) and the read-only diagnostic (`goodall doctor`) need
prompts (text, confirm, choose-from-list, multi-line), tables and readable
failure output in a macOS terminal. All of that goes through one UI module
(AGENTS.md invariant 9). The UI module is not on the hook path, so invariant 7
and ADR 0005 do not apply to it directly. A later ticket decides between a
third-party library and the standard library alone. This file records facts
only and makes no recommendation.

Every fact was checked on **2026-11-16** against the PyPI JSON API (including
PEP 658 wheel metadata), the GitHub REST API (repositories, commits,
contributors, repository security advisories), OSV.dev, and each project's own
documentation. Nothing was installed.

## Summary

- **rich** 15.0.0 renders tables and tracebacks and has typed prompts, but no
  arrow-key list selection. Its company, Textualize, closed in 2025, and Rich
  is now maintained by its author. 3 packages installed.
- **questionary** 2.1.1 has every prompt type needed, but no tables. One
  maintainer, whose day job is elsewhere, and no release for 15 months.
  2 packages installed.
- **prompt_toolkit** 3.0.53 is the engine under questionary and InquirerPy.
  Effectively one maintainer (employed by Cisco). One release in the last
  year. 1 package installed.
- **click** 8.5.1 has typed prompts, confirm, hidden input, `getchar`, editor
  launch for multi-line input and ANSI styling, but no list selection or
  tables. Pallets team; most active of the candidates. Zero dependencies.
- **InquirerPy** 0.3.4 has had no release since 2022-06 and no commit since
  2022-11. One maintainer. 3 packages installed.
- Across all candidates and their dependencies, the only advisory in the last
  two years is a low-severity ReDoS in **Pygments** (a rich dependency),
  fixed in 7 days.
- The **standard library** covers text, confirm, hidden input, terminal width,
  wrapping, ANSI colour and single-key reads. It has no table, no list
  selector, and no wide-character-aware wrapping. On macOS, line editing may
  be backed by libedit instead of GNU readline.

## Comparison

"Installed" counts every package that would be added to a Python 3.12
environment on macOS arm64, including the candidate itself. It is derived from
the `Requires-Dist` lines of each wheel's metadata, not from an install (see
"Could not verify"). None of the packages declares a macOS-only dependency.

| Package | Latest (date) | Owner | Stars | Last commit | Licence | Direct / installed | Advisories, last 2 years |
|---|---|---|---|---|---|---|---|
| **rich** | 15.0.0 (2026-04-12) | `Textualize` org, one public member (Will McGugan) | 57,813 | 2026-11-07 | MIT | 2 / 3 | None for rich. Pygments: 1 low |
| **questionary** | 2.1.1 (2025-08-28) | Personal repo `tmbo/questionary` (Tom Bocklisch) | 2,202 | 2026-06-02 | MIT | 1 / 2 | None |
| **prompt_toolkit** | 3.0.53 (2026-07-26) | `prompt-toolkit` org, one public member (Jonathan Slenders) | 10,620 | 2026-10-20 | BSD-3-Clause | 1 / 1 | None |
| **click** | 8.5.1 (2026-11-06) | `pallets` org, six public members | 17,827 | 2026-11-16 | BSD-3-Clause | 0 / 0 | None |
| **InquirerPy** | 0.3.4 (2022-06-27) | Personal repo `kazhala/InquirerPy` (Kevin Zhuang) | 484 | 2022-11-19 | MIT | 2 / 3 | None |

Dependency trees:

- rich → `markdown-it-py` 4.3.0 → `mdurl` 0.1.2; rich → `pygments` 2.21.0
  (`ipywidgets` only with the `jupyter` extra).
- questionary → `prompt_toolkit` → `wcwidth` 0.9.4.
- prompt_toolkit → `wcwidth`.
- click → nothing (8.5.1 declares no `Requires-Dist` at all).
- InquirerPy → `prompt-toolkit` → `wcwidth`; InquirerPy → `pfzy` 0.3.4.

How each covers the job:

| Need | rich | questionary | prompt_toolkit | click | stdlib |
|---|---|---|---|---|---|
| Text prompt | yes | yes | yes | yes | `input()` |
| Hidden input | yes | yes | yes | yes | `getpass` |
| Confirm | yes | yes | build it | yes | build it |
| Choose from a list | typed choice only | arrow-key `select` and `checkbox` | build it | typed `Choice` only | build it (`tty`/`termios`) |
| Multi-line | no | `text(multiline=True)` | yes | `click.edit()` opens `$EDITOR` | loop over `input()` or read to EOF |
| Tables | yes | no | no | no | build it |
| Styled errors and tracebacks | yes | no | no | `style`/`secho` | ANSI codes by hand |

## Per-candidate notes

### rich

- Version 15.0.0, first uploaded 2026-04-12. Six releases in the last year,
  14.3.0 through 15.0.0. `Requires-Python >=3.9`, "Production/Stable".
  [PyPI JSON](https://pypi.org/pypi/rich/json)
- Repository [Textualize/rich](https://github.com/Textualize/rich): 57,813
  stars, last push 2026-11-13. Of the 100 commits since 2025-11-16, 93 are by
  `willmcgugan`. All-time top contributors: willmcgugan 3,003, darrenburns 315,
  davep 42. The `Textualize` organisation's only public member is
  `willmcgugan`. The PyPI project page lists one maintainer, `willmcgugan`.
- **Change of ownership context.** Textualize, the company that employed the
  Rich and Textual maintainers, announced on 2025-05-07 that it would close
  "in the next few weeks". Will McGugan said he would keep maintaining Rich
  and Textual.
  [The future of Textualize](https://textual.textualize.io/blog/2025/05/07/the-future-of-textualize/)
  His GitHub profile lists no employer.
  [users/willmcgugan](https://github.com/willmcgugan) Dave Pearson (davep),
  one of the next most active past contributors, posted "Goodbye Textualize"
  on 2024-03-28.
  [blog.davep.org](https://blog.davep.org/2024/03/28/goodbye-textualize.html)
  Darren Burns published a post with the same title; its date was not
  checked. The repository stayed under the `Textualize` organisation.
- Licence MIT. Direct dependencies `markdown-it-py>=2.2.0` and
  `pygments>=2.13.0,<3`, from the
  [wheel metadata](https://pypi.org/pypi/rich/15.0.0/json).
  - `markdown-it-py` is maintained by Chris Sewell under
    `executablebooks`. `mdurl` is maintained by Taneli Hukkinen. Its last
    release was 0.1.2 on 2022-08-14.
  - `pygments` 2.21.0 (2026-08-17), maintained by Georg Brandl and Matthäus
    Chajdas, BSD-2-Clause.
- Advisories. There are no repository advisories and no OSV entries for
  `rich`. Its dependencies have these:
  - **Pygments, in the window:** GHSA-5239-wwwm-4pmq / CVE-2026-4539, a ReDoS
    in GUID matching, severity low. Published 2026-03-22 and fixed in 2.20.0,
    released 2026-03-29.
    [GHSA-5239-wwwm-4pmq](https://github.com/advisories/GHSA-5239-wwwm-4pmq)
  - **markdown-it-py, outside the window:** two denial-of-service CVEs
    (CVE-2023-26302 and CVE-2023-26303), fixed in 2.2.0, 2023-02. Rich's
    floor is already `>=2.2.0`.
  - **Pygments, outside the window:** older advisories from 2021 to 2023.
    [OSV query API](https://api.osv.dev/v1/query)
- Prompts. `Prompt`, `IntPrompt`, `FloatPrompt` and `Confirm` exist. Choices
  are typed and re-asked until valid; there is no arrow-key selection.
  [Rich prompt docs](https://rich.readthedocs.io/en/latest/prompt.html)
  `Console.input` supports `password=True` through `getpass`, according to
  [PR #4070](https://github.com/Textualize/rich/pull/4070).
- Import time. These are published figures, measured by contributors on their
  own machines; this research did not measure them.
  - [PR #3399](https://github.com/Textualize/rich/pull/3399), merged
    2024-07-01, cut a Typer-like set of rich imports from about 230 ms to about
    130 ms. That was measured on Windows.
  - [PR #4070](https://github.com/Textualize/rich/pull/4070), merged
    2026-04-11, reports `import rich` at 18 ms. It also reports
    `from rich.console import Console` falling from 78 ms to 52 ms. The
    machine and operating system are not stated. Tables and markup import
    more than `Console` alone.

### questionary

- Version 2.1.1, uploaded 2025-08-28. There has been no release in the last
  year. `Requires-Python >=3.9`. Its classifier is "Development Status :: 4 -
  Beta". [PyPI JSON](https://pypi.org/pypi/questionary/json)
- Repository [tmbo/questionary](https://github.com/tmbo/questionary) is a
  personal-account repository with 2,202 stars and a last commit on
  2026-06-02.
  - Of the 35 commits since 2025-11-16, 22 are from dependabot, 2 from `tmbo`,
    and the rest are one or two each from outside contributors.
  - All-time contributors: tmbo 274, dependabot 132, kiancross 97 (whose
    profile lists the University of Cambridge), xmatthias 21.
- Maintainer. The wheel metadata names Tom Bocklisch as sole author and
  maintainer. His GitHub profile says he is a full-time Principal Architect at
  Rasa and a co-founder of scalableminds.
  [users/tmbo](https://github.com/tmbo) Questionary is not his employer's
  project. Licence MIT.
- One direct dependency, `prompt_toolkit>=2.0,<4.0`. Installed total 2.
- No repository advisories and no OSV entries.
- Prompt types: `text` (with `multiline=True`), `password`, `path`, `confirm`,
  `select`, `rawselect`, `checkbox`, `autocomplete`,
  `press_any_key_to_continue`. There is no table support.
  [Question types](https://questionary.readthedocs.io/en/stable/pages/types.html)
- No published import-time figure was found. It imports prompt_toolkit.

### prompt_toolkit

- Version 3.0.53, uploaded 2026-07-26. This is the only release in the last
  year. `Requires-Python >=3.10`. The licence classifier is "BSD License"; the
  GitHub licence is BSD-3-Clause. One dependency, `wcwidth>=0.1.4`.
  [PyPI JSON](https://pypi.org/pypi/prompt_toolkit/json)
- Repository
  [prompt-toolkit/python-prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit)
  has 10,620 stars, 790 open issues and pull requests, and a last commit on
  2026-10-20.
  - It moved at some point from a personal account to the `prompt-toolkit`
    organisation. The organisation's only public member is
    `jonathanslenders`.
  - Of 15 commits since 2025-11-16, 3 are by `jonathanslenders` and 12 are
    single commits by others.
  - All-time contributors: jonathanslenders 2,291, delirious-lettuce 84,
    Carreau 21.
- Maintainer Jonathan Slenders, whose GitHub profile lists Cisco as his
  company. [users/jonathanslenders](https://github.com/jonathanslenders)
  prompt_toolkit is not presented as a Cisco project.
- `wcwidth` 0.9.4 (2026-10-24) is a single-maintainer package (Jeff Quast),
  MIT, with no dependencies and no OSV entries.
- No repository advisories and no OSV entries for `prompt-toolkit`.
- Import time. [Issue #1592](https://github.com/prompt-toolkit/python-prompt-toolkit/issues/1592)
  ("Improve prompt_toolkit import time", opened 2022-03-14) is still open. It
  gives no number in its text, only a profiler screenshot.
- It is a toolkit: confirm and list selection must be built from its widgets,
  which is what questionary and InquirerPy do.

### click

- Version 8.5.1, uploaded 2026-11-06. Seven releases since 2026-04, from
  8.3.2 to 8.5.1. `Requires-Python >=3.10`. Licence BSD-3-Clause. The wheel
  metadata declares no dependencies; Windows-only `colorama` is no longer
  listed. [PyPI JSON](https://pypi.org/pypi/click/8.5.1/json)
- Repository [pallets/click](https://github.com/pallets/click) has 17,827
  stars and was pushed to on 2026-11-16.
  - At least 100 commits since 2025-11-16; the API page limit was 100. Of
    those, Rowlando13 made 53 and kdeldycke 44.
  - All-time contributors: davidism 878, mitsuhiko 593, Rowlando13 397,
    kdeldycke 211.
  - The `pallets` organisation has six public members: CheeseCake87,
    davidism, kevin-brown, pgjones, Rowlando13, ThiefMaster.
- Maintainers and employers. Pallets is a volunteer team that takes
  donations, according to the PyPI project URLs. From GitHub profiles:
  - David Lord: core Pallets developer, no employer listed.
  - Rowlando13: Wallstreet.io.
  - Kevin Deldycke: no employer listed.
- No repository advisories and no OSV entries for `click`.
- What it provides for this job:
  - `prompt` (with `hide_input` and a typed `Choice`), `confirm`, `echo`,
    `style` and `secho` for ANSI colour, `getchar` for single raw keys,
    `edit` to open the user's editor for multi-line input, `progressbar`,
    `echo_via_pager`.
  - Arrow keys come back from `getchar` as raw escape sequences; there is no
    menu widget and no table.
  [Click utilities](https://click.palletsprojects.com/en/stable/utils/)
- No published import-time figure for click itself was found.

### InquirerPy (considered, appears dormant)

- Version 0.3.4, uploaded 2022-06-27. Last commit 2022-11-19; repository
  [kazhala/InquirerPy](https://github.com/kazhala/InquirerPy), 484 stars, MIT.
  No commits in the last year.
- Maintainer Kevin Zhuang, whose profile lists Canva. He made 719 of the
  roughly 730 commits.
- Dependencies `prompt-toolkit>=3.0.1,<4` and `pfzy>=0.3.1,<0.4`. `pfzy` is
  the same author's fuzzy matcher, with no dependencies.
  [PyPI JSON](https://pypi.org/pypi/inquirerpy/json)
- No advisories.

### Not assessed

- **textual** was excluded by the ticket: full-screen apps are out of scope.
- Other small menu packages, such as `simple-term-menu` and `pick`, were not
  examined.

## The standard library alone

All citations are to the Python 3.12 documentation.

- **Text input.** `input(prompt)` writes the prompt, reads one line and raises
  `EOFError` at end of input. It gains line editing and history only "if the
  `readline` module was loaded".
  [input()](https://docs.python.org/3.12/library/functions.html#input)
- **Hidden input.** `getpass.getpass()` prompts on the controlling terminal
  (`/dev/tty`), or on `sys.stderr` if that is unavailable. If echo-free input
  is unavailable, it falls back to reading `sys.stdin` with a visible echo
  and issues `GetPassWarning`.
  [getpass](https://docs.python.org/3.12/library/getpass.html)
- **Terminal size.** `shutil.get_terminal_size()` uses `COLUMNS` and `LINES`
  first. If they are unset, it asks the terminal on `sys.__stdout__`. If that
  fails, or the output is not a terminal, it returns the fallback `(80, 24)`,
  which since 3.11 is also used when the size comes back as zero.
  [shutil.get_terminal_size](https://docs.python.org/3.12/library/shutil.html#shutil.get_terminal_size)
- **Wrapping.** `textwrap` measures width in characters ("at most *width*
  characters long"). The documentation says nothing about wide (East Asian)
  characters, emoji or ANSI escape sequences. Coloured or wide text therefore
  wraps by character count, not by display columns.
  [textwrap](https://docs.python.org/3.12/library/textwrap.html)
- **Line editing on macOS.** The `readline` module "may be implemented by the
  `libedit` library instead of GNU readline", and on macOS it detects which at
  run time.
  - libedit reads `~/.editrc`, not `~/.inputrc`, with a different syntax.
  - History file formats can differ between the two libraries.
  [readline](https://docs.python.org/3.12/library/readline.html)
- **Single keys and list selection.** `tty.setcbreak()` and `tty.setraw()`
  (Unix only, built on `termios`) allow reading one key at a time. An
  arrow-key list selector would have to parse escape sequences and redraw
  lines itself, and restore terminal settings on every exit path.
  [tty](https://docs.python.org/3.12/library/tty.html)
  `curses` exists but takes over the whole screen.
- **Colour.** The standard library has no public API for styling output.
  Colour means writing ANSI escape codes by hand and deciding when to suppress
  them, for example when output is not a TTY or `NO_COLOR` is set.
- **Missing entirely:** tables, confirm and select widgets, multi-line editing
  inside the terminal, and display-width measurement.
  `unicodedata.east_asian_width` gives a per-character width class to build
  one from.

## Could not verify

Recorded so that "not stated" is never mistaken for "confirmed safe".

- **Installed dependency counts.** They come from wheel `Requires-Dist`
  metadata, not from a real resolve on macOS arm64 under Python 3.12, because
  installing was out of scope.
- **PyPI owner lists.** The lists for questionary, prompt-toolkit, click,
  InquirerPy, markdown-it-py, mdurl and pygments could not be read: the PyPI
  pages did not render for automated fetches. Only rich (`willmcgugan`) and
  wcwidth (`jquast`) were confirmed.
- **Commit access.** Whether anyone other than Will McGugan has commit or
  release rights on rich since Textualize closed.
- **Maintainer funding.** Whether Will McGugan is now employed or funded, and
  whether rich has any formal succession arrangement.
- **prompt_toolkit's move.** When and why it moved from a personal account to
  the `prompt-toolkit` organisation, and whether anyone else holds release
  rights.
- **Pallets.** Whether it has a fiscal host or paid maintainers.
- **Import times.** Credible figures for questionary, prompt_toolkit, click
  or InquirerPy, and a macOS figure for rich at version 15.0.0.
- **Line editing on goodall's interpreter.** Whether the Python builds goodall
  will run on (python.org installer, or uv-managed python-build-standalone)
  link GNU readline or libedit on macOS. This decides how `input()` editing
  behaves.
- **Private reports.** Whether any of these projects has received security
  reports that never became published advisories.
