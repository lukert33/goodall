# Setup happens on a page with no server, and the terminal only prints

Goodall asks its setup questions on a page it opens in the person's browser, not in the terminal, because part of its audience is not at home in a terminal and a page offers what no terminal library does: a real list to choose from and a text box that can be edited freely. The page has nothing listening behind it. `goodall onboard` writes the page with a one-time code inside it, opens it, and waits; pressing the page's one button saves an answers file; the waiting command finds that file, accepts it only if it carries the same code, checks the answers against the machine, writes the files, and reports in the terminal. A page served by a small local web server was considered, since it could show the result itself, and rejected: a local service on a machine that holds credentials is an attack surface other programs and web pages can reach, and the cost of doing without it is one glance back at the terminal. With questions gone from the terminal, terminal output is built on the standard library alone; the terminal libraries considered each had one effective maintainer or offered little beyond what a hundred lines of our own code does.

## Consequences

- The page cannot see the machine, so it cannot tell whether a folder exists; goodall checks when it applies the answers and says so in the terminal.
- If the browser saves the file somewhere unexpected, goodall says so after a short wait and accepts the file's path.
- Unattended setup passes an answers file to `goodall onboard` directly.
- The page never accepts a secret value, only secret references, by the same check as the env file.
- A command builds its results once; the terminal report and `--json` output are two renderings of the same records, and a failure is always what happened, why, and what to do next.
