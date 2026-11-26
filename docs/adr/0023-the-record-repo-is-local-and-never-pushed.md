# The record repo is local and never pushed

The record repo is a git repository on the person's Mac that goodall commits to and never pushes. Goodall adds no remote, fetches nothing, and has no code that sends a record anywhere; the only way anything leaves the Mac is an export the person makes (ADR 0017). Until now goodall committed and pushed to a remote whenever one was set (ADR 0001), so that a team could share an eval's records and a researcher had a copy off the machine. We reversed that because the current build is about the local record, and the builds after it are about the export, with its levels of richness, permission and redaction. A push that happens by itself is an upload of times, workstream names and a person's own written notes, and it made it impossible to tell a new user the plain sentence they most need to hear: nothing is uploaded. We kept git, although nothing is pushed, because local history is what makes a later edit to one of goodall's own records visible. Keeping the push as an option that is off by default was rejected: an option nobody needs yet is code to keep working and a promise to qualify.

## Consequences

- The record repo is the local store for what goodall and the person write on purpose: work blocks, journal entries, interventions and the eval config. With the raw records beside it in the log directory, it feeds the timeline, the local reporting and the exports.
- All pushing, fetching, merging after a rejected push and remote checking is removed, with the "push pending" warnings (`goodall-g1k.5`). ADR 0001's sentence that goodall commits and pushes, and ADR 0009's that the record repo's remote is read from git, no longer hold.
- GitHub's `gh` is no longer needed. Its one use was letting `goodall doctor` check a GitHub sign-in on goodall's behalf (ADR 0011). `git` stays a required program.
- If a person adds a remote to the record repo by hand, goodall leaves it alone and does not push to it. `goodall doctor` says so in plain words.
- An eval is one person's record on one Mac. The roster, the contributor id on every record, and the split between eval config and local settings (ADR 0009) all stay, so that several people's records can be associated later. That association will happen between exports, not by sharing a repository.
- There is no copy of the record off the Mac unless the person makes one. Backing up is the person's job, with whatever they already use for their Mac, and the docs say so.
- `goodall teardown`'s "final off-machine copy of the records" step becomes a final export.
