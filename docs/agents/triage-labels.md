# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those
roles to the label strings used in this repo's issue tracker, Beads. Here they
are identical.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (for example "apply the AFK-ready triage label"),
use the corresponding label string from this table, applied with
`bd label add <id> <label>`.

The two category roles map to Beads issue types, not labels: `bug` is type
`bug`, and `enhancement` is type `feature`.

Beads labels need no setup; a label exists as soon as an issue carries it.
These labels are separate from Beads' own statuses (`open`, `in_progress`,
`blocked`, `closed`), which keep their usual meaning. A `wontfix` issue is
labeled and then closed.
