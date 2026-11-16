# Domain Docs

How the engineering skills should consume this repo's domain documentation
when exploring the codebase. This is a single-context repo.

## Before exploring, read these

- **`GLOSSARY.md`** at the repo root.
- **`docs/adr/`**: read the ADRs that touch the area you are about to work in.

## File structure

```
/
├── GLOSSARY.md
├── docs/adr/
│   ├── 0001-installed-tool-with-a-separate-record-repo.md
│   └── ...
└── src/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor
proposal, a hypothesis, a test name), use the term as defined in
`GLOSSARY.md`. Stay with the chosen term; the glossary lists the synonyms it
avoids.

If the concept you need isn't in the glossary yet, that's a signal: either
you're inventing language the project doesn't use (reconsider) or there's a
real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than
silently overriding:

> _Contradicts ADR-0002 (own telemetry receiver), but worth reopening because…_

## What goes where

- **`docs/adr/`**: decisions that are hard to reverse, surprising without
  context, and the result of a real trade-off. One short paragraph each.
- **`GLOSSARY.md`**: terms only, with no implementation detail.
- **Beads memory** (`bd remember`, `bd memories <keyword>`): working notes and
  spike findings, such as observed behaviour of a tool at a given version.
