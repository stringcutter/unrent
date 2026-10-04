# Security

unrent reads files and prints a report. It makes no network requests while scanning
and runs only `git ls-files` and, if installed, `rg` as subprocesses.

Please report a vulnerability privately through GitHub:
**Security → Report a vulnerability** on this repository
(https://github.com/stringcutter/unrent/security/advisories/new). Do not open a
public issue. Expect a reply within a week.

In scope: anything that makes a scan execute code from the scanned repository, write
outside `--output`, or print an unmasked secret in a report.

Out of scope: detection misses and false positives (open a normal issue), and the
licences or security of the listed open source alternatives.
