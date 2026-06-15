# OpenClaw Feishu Sidecar

This directory pins the external OpenClaw runtime used for the Feishu channel.
The Python support copilot does not vendor or import OpenClaw code directly.

Pinned versions:

- `openclaw@2026.6.6`
- `@larksuite/openclaw-lark@2026.6.10`

Runtime requirement:

- Node.js `22.22.2` is the recommended runtime for this lockfile.
- npm `11.17.0` via Corepack.

Local setup used for this lockfile:

```bash
nvm install 22.22.2
nvm use 22.22.2
corepack prepare npm@11.17.0 --activate
corepack npm install --package-lock-only --ignore-scripts
```

The current dependency audit reports high-severity issues through
`@larksuite/openclaw-lark -> @larksuiteoapi/node-sdk -> axios`, with no npm
fix available at the time this lockfile was generated. Treat OpenClaw upgrades
as explicit dependency-review PRs rather than ad hoc runtime updates.
