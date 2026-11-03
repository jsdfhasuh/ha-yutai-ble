# Third-Party Notices

The project MIT license applies to original integration, tooling and documentation
code. Bundled third-party files retain their own licenses and notices.

## BLE protocol

`custom_components/yutai_ble/protocol/vendor/channel.py` and
`miot_ble_specv2.py` are from xiaomi-diffuser-ble contributors (2026), MIT.
The original license is retained at `protocol/vendor/LICENSE`. Their exact bytes
are hash-checked by the worker. The wrapper replaces the command and lifecycle
paths; it does not modify these vendor files.

The same pinned vendor sources and MIT license are also retained independently
in `research/yutai_fsov7m/vendor` and `research/wiing_v7icm/vendor`.
Upstream: https://github.com/clairefiorettiythzr6802-eng/xiaomi-diffuser-ble
Commit: `96268a53e3c8ba4443dd322ecaede606ec7c89c6`.
The research wrappers originated in model-specific device experiments; their
published copies replace personal addresses with validated per-request targets.

## Optional QR helper

`tools/auth/vendor/cloud.py` is an asynchronous Xiaomi QR login implementation
carried from the xiaomi_s400 project under Apache-2.0. The enclosing project
license is retained at `tools/auth/vendor/LICENSE`. The file's header attributes
the original QR flow to PiotrMachowski/Xiaomi-cloud-tokens-extractor (MIT).
That license is also retained as `tools/auth/vendor/LICENSE.tokens-extractor`.
This vendor file is unmodified; the local loader uses the bundled file.

Home Assistant, Xiaomi, Mi Home and Yutai names identify compatibility only.
This is an unofficial project, not endorsed by those projects or manufacturers.
