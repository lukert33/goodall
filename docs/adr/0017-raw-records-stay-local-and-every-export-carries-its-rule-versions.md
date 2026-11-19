# Raw records stay local, and every export carries its rule versions

Raw records never leave the Mac they were captured on: a person shares only a redacted layer or a derived layer, never the raw one. Goodall ships a default redaction policy, and each user may make it stricter or looser for their own eval, because what is private differs between a solo founder and a team with customers. Every export is stamped with the version of the redaction policy applied and the version of each derivation rule used to compute it, so that two people's exports can be compared, or refused as not comparable, without seeing either person's raw data. Redaction must keep where each error was caught (ADR 0018) even when the content around it is removed, since that is the first thing an analyst will ask of the data. We rejected publishing raw logs with names removed, which was the original plan: transcripts hold customers' words, window titles and page addresses, and no pattern-matching pass can be trusted to find all of it. We also rejected a single fixed policy: it would be too loose for some evals and would stop others sharing anything.

## Consequences

- Any tool goodall builds on must be able to keep its data on the Mac. A tool that sends session records to a git remote or a service by default can be used only if that can be turned off (see `docs/related-work.md` on Entire).
- The record repo keeps holding only what a person wrote on purpose (work blocks, journal entries, interventions). Raw records stay in the log directory (ADR 0015).
- An export without its stamp is not a goodall export. The stamp names the redaction policy version, each derivation rule version (actor labels, human actions, and any later layer), and the goodall version.
- Redaction is a derivation: it is versioned, documented, and can be run again on the same raw record with a different policy.
- Pooling data across users is possible only at the redacted or derived layer, and only between exports whose stamps agree.
