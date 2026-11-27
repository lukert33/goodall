# The answers file, and `goodall onboard --answers`

An **answers file** holds one person's answers to goodall's setup questions
(GLOSSARY.md). Plain `goodall onboard` opens the setup page, which saves one
when you press its Finish setup button ([setup-page.md](setup-page.md),
ADR 0012). For unattended setup, or to set up again from a file the page
saved, write or keep the answers file yourself and run:

```
goodall onboard --answers ~/goodall-answers.toml --plan
goodall onboard --answers ~/goodall-answers.toml
```

The first command shows every change the answers call for and makes none.
The second shows the same plan and then makes those changes, and nothing
else. Goodall never asks a question in the terminal, so **supplying the
answers file is the go-ahead**: look at the plan with `--plan` first.

`--json` prints the result as one JSON document instead (see the end of this
page).

## What goodall onboard sets up

This is Parts 4 and 5 of [setup-by-hand.md](setup-by-hand.md), done for you.
In the order goodall does it:

| Change | What | For an existing eval |
|---|---|---|
| `record_repo` | The record repo folder, made a git repository with **no remote**. Goodall commits to it and never pushes (ADR 0023). | Must already be there, and be a git repository. |
| `eval_config` | `eval.toml` in the record repo, with you as the one contributor. | Left exactly as it is. You must already be in its roster. The one exception: names added in `[existing_eval]` (below) join its `workstreams` line. |
| `construct` | `CONSTRUCT.md` in the record repo, when the answers give a construct. | Not written. |
| `record_repo_commit` | One commit of `eval.toml` and `CONSTRUCT.md`, and nothing else. | No commit, unless a workstream was added: then one commit of `eval.toml` alone. |
| `log_directory` | The log directory, which only this macOS user account can open. | The same. |
| `env_file` | The env file, `.env` beside local settings, readable by this account only, holding secret references only. | The same. |
| `local_settings` | `local-settings.toml` in `~/.config/goodall/`. | The same. |
| `claude_code_hooks` | Goodall's five hooks in Claude Code's user settings, `~/.claude/settings.json`, beside anything already there ([claude-code-hooks.md](claude-code-hooks.md)). | The same. |

The hooks come last: in dedicated account mode, once they are in, Claude
Code refuses prompts until you run `goodall start`.

Goodall onboard does **not** do these; they stay by-hand steps in
[setup-by-hand.md](setup-by-hand.md):

- Create the eval account, or install anything for the whole Mac (Part 1).
- Install goodall, git's name and email, and Claude Code (Part 2). Goodall
  needs git's name and email for the record repo's first commit, and stops
  before changing anything if git has none.
- Give ActivityWatch its own port (Part 3). When the eval includes
  ActivityWatch, follow Part 3, then add its port and identifier to the
  answers file (below) and run goodall onboard again. Until then the plan
  warns that `goodall doctor` will report them missing.
- Write the shared env file, `.env.shared`. What it should hold is not
  decided yet, so `goodall doctor` keeps warning that it is missing.

## Running it again

Running goodall onboard again is safe:

- **With the same answers, nothing changes**, and the plan says
  `unchanged` on every line.
- **With changed answers, only what differs changes.** In local settings and
  the env file, a setting that differs has its one line replaced, and a
  setting that is missing is added at the end under a comment saying goodall
  onboard added it. Every other line, your own comments included, stays as
  it was. The plan names each setting it changes, never a secret
  reference's value.
- **`eval.toml` and `CONSTRUCT.md` are never rewritten.** They are committed
  records you may have edited on purpose, such as adding a workstream. If
  one says something other than the answers, the plan warns, leaves it as
  it is, and the eval goes on using what the file says. To change one, edit
  it and commit the change with git in the record repo.
- **Claude Code's settings** keep every setting they had. Goodall adds each
  of its hooks that is missing, and replaces an older goodall hook (one
  that runs another goodall, say) rather than adding a second. The file is
  written back as indented JSON.
- A setting the answers leave out (such as the ActivityWatch ones) is left
  as it is in local settings.
- If a change fails part way, goodall stops there and says which changes it
  made. Fix what it names and run the same command again: it makes only
  what is still missing.

## The format

An answers file is TOML. The setup page writes exactly this format; it
leaves out `[activitywatch]` (set up by hand, Part 3 of
[setup-by-hand.md](setup-by-hand.md)), always uses `paths = ["**"]` for
each scope repository, and writes `[claude_code]` only when the hooks
already run a goodall by its full path. Every value below is an example; use your own. Keys
not marked optional are required.

```toml
# A goodall answers file. Run: goodall onboard --answers <this file>
schema_version = 1

# "dedicated" for an eval account, used for this eval and nothing else (the
# recommended setup), or "mixed" for a macOS user account you also use for
# other things, the "Try it out" choice (ADR 0021).
account_mode = "dedicated"

# "new" to start a new eval in a new record repo, or "existing" to use the
# record repo, and its eval.toml, already on this Mac.
eval = "new"

# Only with eval = "existing", and optional: the eval's whole workstream
# list. Goodall adds the names eval.toml lacks to its workstreams line, in
# the order given after the ones already there, and commits that change in
# the record repo. It never removes or reorders one: a list that leaves out
# a workstream eval.toml has is refused. `goodall settings` saves this.
# [existing_eval]
# workstreams = ["engineering", "research", "go-to-market"]

# Only with eval = "new": what becomes eval.toml and CONSTRUCT.md.
[new_eval]
name = "goodall-pilot"
description = "Building a product to first recurring revenue with AI agents."
workstreams = ["engineering", "research", "go-to-market"]
# Optional; these four are the default.
intervention_categories = ["policy-mandated", "infrastructure", "agent-limitation", "freely-delegable"]
# Optional; false when left out. See Part 3 of setup-by-hand.md.
activitywatch = false
# Optional. When given, goodall writes CONSTRUCT.md.
construct = """
Which parts of the work needed to reach first recurring revenue can be
delegated to AI agents, and which human interventions are indispensable?
"""

# At least one: the repositories the eval's work is in, by identity.
[[new_eval.scope]]
repository = "github.com/your-org/goodall-pilot-app"
paths = ["**"]

[contributor]
id = "ada-lovelace"
# Only with eval = "new": the name shown beside your work in the roster.
display_name = "Ada Lovelace"
main_workstream = "research"

[folders]
record_repo = "~/evals/goodall-pilot-records"
log_directory = "~/evals/goodall-pilot-logs.noindex"

# Optional. Leave it out for an eval that uses no secrets.
[secrets]
manager = "1password"   # optional; 1password is the only one supported

[secrets.references]
ANTHROPIC_API_KEY_RESEARCH = "op://goodall-eval/anthropic-research/credential"

# Optional: this account's own ActivityWatch, from Part 3 of setup-by-hand.md.
[activitywatch]
port = 5610
device_id = "8d3f2c1e-4b5a-4c6d-9e7f-0a1b2c3d4e5f"

# Optional: the goodall the Claude Code hooks run, as command -v goodall
# prints it. Left out, goodall uses the goodall it finds on the PATH.
[claude_code]
goodall_program = "/Users/ada/.local/bin/goodall"
```

The rules, which are those of the files the answers become, so that goodall
onboard can never write a file that `goodall status` or `goodall doctor`
would refuse:

- Names (the eval's `name`, `id`, workstreams, intervention categories) are
  lowercase letters, digits and hyphens, such as `go-to-market`.
  `unknown` is not a workstream: goodall keeps it for itself.
- `main_workstream` must be one of the eval's workstreams.
- Folders are full paths, starting with `/` or `~/`. The record repo and
  the log directory are two separate folders, neither inside the other,
  because the log directory holds every transcript and must never be
  committed. For a new eval, the record repo folder must not exist yet, or
  be empty, or already be a git repository; goodall does not turn a folder
  of your files into a repository. The log directory's name should end in
  `.noindex`, so Spotlight leaves goodall's copies out of your searches
  (ADR 0019); the plan warns when it does not.
- A scope `repository` is `host/owner/name`, never a path or a web address,
  and `paths` are patterns inside it.
- `[existing_eval]` is the only way an answers file changes an eval.toml
  goodall did not write, and it changes one line of it, the workstreams
  (docs/setup-page.md, "Settings"). Goodall refuses it when eval.toml has
  uncommitted edits, so its commit never carries yours, and when the
  workstreams are not written as one whole line, in which case it says to
  add the name by hand.
- Nothing in `[new_eval]` or `display_name` may be an email address: the
  eval config is made to be published as it stands (ADR 0009).
- `activitywatch` `port` may not be 5600, ActivityWatch's default (ADR 0016).

### No secrets, only references

**An answers file never holds a secret.** Each line under
`[secrets.references]` names an environment variable and gives a
**secret reference** into 1Password (`op://vault/item/field`), never the key
itself. Goodall writes those lines into the env file, readable by this
account only, and nowhere else (ADR 0010).

Goodall refuses the whole answers file, and changes nothing, when:

- a value under `[secrets.references]` is not a well-formed reference;
- any value anywhere in the file looks like a key in a format goodall
  recognizes (an Anthropic, OpenAI, GitHub, AWS, Slack or Google key, a
  1Password service account token, a private key or a JSON Web Token);
- a secret reference appears anywhere other than `[secrets.references]`;
- `[secrets.references]` sets `OP_SERVICE_ACCOUNT_TOKEN`, which can never be
  a reference.

Goodall never repeats a refused value in what it prints. If you did paste a
real key, replace it with whoever issued it.

### The one-time code

The setup page writes a `one_time_code` line into the answers file it
saves, so the waiting `goodall onboard` uses only the file from the page it
opened ([setup-page.md](setup-page.md)). `goodall onboard --answers` expects no code: a `one_time_code` line
is accepted and ignored, so a file the page saved can also be used
unattended.

## `--json`

With `--json`, standard output carries one JSON document and nothing else.
`--json` needs `--answers`: plain `goodall onboard` opens a page for a
person, so with `--json` and no answers file it is a usage error (exit 2).

```json
{
  "answers_file": "/Users/ada/goodall-answers.toml",
  "plan_only": false,
  "outcome": "applied",
  "changes": [
    {
      "id": "local_settings",
      "what": "Local settings",
      "path": "/Users/ada/.config/goodall/local-settings.toml",
      "action": "create",
      "details": ["contributor ada-lovelace, in dedicated account mode"],
      "done": true
    }
  ],
  "warnings": [],
  "failures": []
}
```

- `outcome` is `applied` (changes made), `nothing_to_change`, `planned`
  (`--plan`), `rejected` (the answers or the machine stopped it before
  anything changed) or `stopped` (a change failed part way).
- Each change's `id` is one of the ids in the table above, `action` is
  `create`, `change` or `unchanged`, and `done` says whether this run made
  it.
- `warnings` and `failures` are goodall's usual failures: `kind`,
  `what_happened`, `why`, `what_to_do_next`, and sometimes
  `commands_to_try`.

The exit code is 0 when the changes were made, or there was nothing to
change, or `--plan` showed the plan; 1 when the answers were refused or a
change failed; 2 for a mistake on the command line.
