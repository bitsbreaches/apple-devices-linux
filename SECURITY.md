# Security policy

## Supported versions

Security fixes are issued for the latest release. Older releases are not supported.

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue:

* Use GitHub's [private vulnerability reporting](https://github.com/bitsbreaches/device-dock/security/advisories/new)
  for this repository.

Include the version, your distribution, the steps to reproduce, and what an attacker could
do. If the report involves device data, remove identifiers, serial numbers and backup
contents from screenshots and logs.

You can expect an initial response within a reasonable time. Please allow time for a fix and
a release before disclosing the issue publicly.

## Scope

Device Dock handles sensitive data: iOS backups, decrypted manifests, photographs,
messages, contacts and device credentials on your own machine. Reports are especially
welcome for:

* Path traversal or unsafe extraction when exporting files.
* Password handling, log redaction and keyring storage.
* Privilege boundaries: the application must never require root.
* Command execution through device-supplied data.

## Design properties that are not vulnerabilities

* Backups are readable once their password is known; that is how Apple's backup format
  works, and Device Dock only reads them.
* Writing to a device (file transfer, restore, factory reset) requires an explicit user
  confirmation; the confirmations are a safety feature, not an authentication barrier.
* There is no telemetry, account system or remote service, so there is no server-side
  attack surface to report.
