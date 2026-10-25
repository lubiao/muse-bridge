# Third-party sources and licensing

The bridge is distributed under Apache-2.0, with original notices preserved for upstream portions. This file does not relicense third-party programs.

| Upstream | Relationship | License / included notice |
|---|---|---|
| [nikships/muse-cli](https://github.com/nikships/muse-cli) 0.3.2 | Python dependency. Muse authentication and Gateway initialization adapted in each adapter's `muse.py`; bounded sockets, framing, callbacks and attachment handling modified for this bridge. | MIT, Copyright (c) 2026 Nik Ships. [Full notice](licenses/muse-cli-MIT.txt), copied from the installed 0.3.2 distribution. |
| [facebookincubator/muse-gadget-sdk](https://github.com/facebookincubator/muse-gadget-sdk) | Framing protocol reference for `noise_framing.py`; modified protobuf-based Python implementation, stricter buffering bounds and bridge integration. | Apache-2.0, Copyright (c) Meta Platforms, Inc. and affiliates. [Full license](licenses/muse-gadget-sdk-Apache-2.0.txt). |
| [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) | iLink QR/API/CDN protocol and encrypted media behavior adapted into Python. No complete OpenClaw runtime is included. | MIT, Copyright (C) 2026 Tencent. [Full notice](licenses/openclaw-weixin-MIT.txt). |
| [wong2/muse-client](https://github.com/wong2/muse-client) | Research reference for sending image items. Not a runtime dependency; its source code is not bundled. | Upstream Apache-2.0; consult its LICENSE/NOTICE if incorporating source in a later release. |
| [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ) | Optional external QQ runtime. Bridge implements OneBot API calls. No NapCat source, binaries, modified build, QQ executable, or login data is distributed here. | External restrictive custom license. [Version 4.18.41 license reviewed](https://github.com/NapNeko/NapCatQQ/blob/v4.18.41/LICENSE); includes non-commercial and modified-code publication restrictions. Obtain and use separately under its own terms. |

The Meta and Tencent license texts were retrieved from upstream on 2026-10-22. Packaged copies are retained to make notices independent of future upstream changes. Other installed Python dependencies include their own license metadata in their distributions. This candidate does not vendor their source.

Upstream names identify compatibility and provenance only. Service access and trademarks are not granted by this project's license. Each operator supplies their own authorized service credentials.
