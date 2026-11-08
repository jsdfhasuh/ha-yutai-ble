# Changelog

## 0.6.2

- Translate model choices in the configuration form while preserving their
  original protocol values and existing device configuration.
- Add Chinese field help, actionable configuration errors and polling guidance;
  keep the English localization available for English-language Home Assistant.
- Localize user-facing control and setup errors without changing BLE operations,
  retry behavior or machine-readable diagnostic codes.
- Document the Chinese model names and every setup field in the README.

## 0.6.1

- Let a background read yield while waiting for the global connection queue,
  so a manual command is not trapped behind its device's queued poll.
  Preserve FIFO order, disconnect cooldowns and active transactions.
- Publish diagnostics after consecutive read or command failures, including
  the first failed poll after a failed command, without marking the device available.
- Check the Windows helper's dependencies before launching; retry setup when
  a previous dependency installation failed after creating the virtual environment.
- Add regressions for nested queues and repeated failures, plus CI checks using
  Home Assistant 2026.10.4 and the actual Windows command interpreter.

Existing configuration entries and entity unique IDs are unchanged. Update the
integration files and restart Home Assistant; no new pairing or key scan is needed.
Radio performance and real-device recovery remain outside this release's validation.

## 0.6.0

- Prioritize manual requests over queued polls, both per entry and across entries.
  Preserve FIFO order between manual commands; never interrupt active BLE work.
- Yield a background read's connect-retry wait or pre-connect cooldown when a
  manual command is pending. Keep read retry bounds and disconnect cooldowns.
- Reuse a newer verified full snapshot for an overlapping queued refresh or
  pending recovery, avoiding another connection for the same readback.
- Coalesce consecutive identical overlapping manual commands only after the
  preceding command has succeeded. Preserve ON/OFF/ON and later explicit requests.
- Keep uncertain writes unavailable, never automatically resend writes, and
  drain cancelled workers before releasing the shared connection gate.

Scheduling changes are validated offline only. Each remaining transaction still
connects, authenticates, reads back and disconnects; no persistent BLE session
or cryptographic-session reuse is introduced. Live latency is not yet measured.

## 0.5.0

- Add ordinary Yutai `yutai.plug.fsov7m` to the HA configuration flow.
- Dispatch a separate fsov7m codec for relay control, indicator follow/off,
  and power-on off/on/memory settings. Do not infer fsov8m-only capabilities.
- Hide metering, DND and double-click entities on fsov7m; reject mismatched
  capabilities again at the HA boundary.
- Share existing serialized transport, read backoff and no-write-retry protections.
- Allow the Windows key-copy helper to handle both Yutai models.

Ordinary-model timers remain research-only. This adapter is tested offline;
no live HA deployment, mixed-model hardware test or relay action was performed
as part of this release. Existing entity unique IDs and entry data are unchanged.

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
