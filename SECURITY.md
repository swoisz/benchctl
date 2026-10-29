# Security policy

Report a vulnerability privately: open this repository's **Security** tab and choose
**Report a vulnerability**. Please don't open a public issue for it.

Fixes land on `main`; there are no maintained release branches.

benchctl runs on a developer's machine. It opens serial ports, keeps one daemon per unit
listening on a Unix socket under the state directory, redacts secrets before anything is
stored, and runs scenario `external` steps as shell commands. Reports that cross one of
those boundaries are the most useful.
