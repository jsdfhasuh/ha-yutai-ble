# Changelog

## 0.4.0

- Package as a HACS custom repository and separate manual-install ZIP.
- Accept multiple fsov8m devices with per-entry addresses and credentials.
- Bind successful report validation and BlueZ lookup to each request's identity.
- Retain global BLE serialization, disconnect cleanup, read backoff and no write retries.
- Bundle an optional portable Windows QR tool; keep keys out of distribution files.
- Add Chinese installation, upgrade, rollback and compatibility documentation.
- Publish separate fsov7m and wiing v7icm research sources with sanitized,
  per-request identities and offline tests. These are not added to the HA model allowlist.

This release has offline tests only. Multi-device physical operation and the
packaged relay control path have not received real-hardware acceptance testing.

## 0.3.0

- Earlier device-specific deployment: metering, seven verified settings,
  guarded relay control and connection diagnostics.
