# The eval config is shared and local settings are not

Settings are split by one test: is this true for every contributor to the eval? What passes goes in the eval config, in the record repo: the eval's name, the roster of contributors, workstreams, declared scope and intervention categories. What is true only for one eval account on one machine goes in local settings: where the record repo and log directory are, which contributor the account is, and its references into a secrets manager. The two hold different keys and local settings never override the eval config, because an override would let two contributors' records disagree about what a workstream is. Secret references stay local even though a shared file would let a team agree on one vault layout once: a reference points into one person's secrets manager, vault names and managers can differ between people, and keeping references and email addresses out of the eval config means it can be published as it stands.

## Consequences

The declared scope names repositories by identity, never by a local path. The record repo's remote is read from git and stored nowhere else. Facts about a machine (hostname, operating system, tool versions) are written onto each work block as observations and appear in neither file.
