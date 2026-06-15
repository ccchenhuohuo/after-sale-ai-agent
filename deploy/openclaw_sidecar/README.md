# OpenClaw Feishu Sidecar

This directory pins the external OpenClaw runtime used for the Feishu channel.
The Python support copilot does not vendor or import OpenClaw code directly.

Pinned versions:

- `openclaw@2026.6.6`
- `@larksuite/openclaw-lark@2026.6.10`

Runtime requirement:

- Node.js `>=22.19.0`

The current dependency audit reports high-severity issues through
`@larksuite/openclaw-lark -> @larksuiteoapi/node-sdk -> axios`, with no npm
fix available at the time this lockfile was generated. Treat OpenClaw upgrades
as explicit dependency-review PRs rather than ad hoc runtime updates.
