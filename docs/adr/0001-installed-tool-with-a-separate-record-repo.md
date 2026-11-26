# Installed tool with a separate record repo

Goodall is installed as a tool and holds no data about any eval; each eval's own records (config, construct, journal, interventions, work blocks) live in a separate record repo that goodall operates, and raw logs live in a log directory outside any repo. The earlier design was a scaffold repo that researchers forked, which mixed one researcher's data into the tool's history, made tool updates a merge, and assumed an eval is one repo when the work spans many. Goodall commits and pushes to the record repo but never creates the remote or sets its visibility, so the researcher keeps control of where their records go and who can see them.

Amended 2026-11-26 (owner): goodall no longer pushes. The record repo is local, and nothing leaves the Mac except an export (ADR 0023).
