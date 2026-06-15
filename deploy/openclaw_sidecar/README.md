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

## Support Copilot contract smoke

The sidecar contract is:

```text
OpenClaw Feishu message/resource
  -> POST /channels/openclaw-feishu/support-case
  -> Support Copilot runtime
  -> OpenClaw thread reply payload
```

Run the Python API first, for example:

```bash
uvicorn agent_runtime.feishu.webhook:app --host 127.0.0.1 --port 8000
```

Confirm the channel endpoint is reachable without invoking the Agent:

```bash
curl -s http://127.0.0.1:8000/channels/openclaw-feishu/health
```

Run the combined OpenClaw + Support Copilot diagnostic before live validation:

```bash
nvm use 22.22.2
corepack npm run doctor:support-copilot -- --allow-unconfigured
```

Drop `--allow-unconfigured` in a configured sidecar environment. Then Feishu
channel config, plugin list, channel status, and Support Copilot health are all
required to pass.

Then run the local OpenClaw-shaped contract smoke:

```bash
nvm use 22.22.2
corepack npm run smoke:support-copilot
```

Optional environment variables:

- `SUPPORT_COPILOT_URL`: defaults to `http://127.0.0.1:8000/channels/openclaw-feishu/support-case`
- `OPENCLAW_FEISHU_BRIDGE_SECRET`: sends the `x-openclaw-feishu-secret` header when configured

The smoke payload includes a two-message burst with text plus an image resource
whose download is marked as failed. A successful response must be a Feishu
thread reply payload with `mode=thread_reply`, `replyInThread=true`, and a
readable fallback text.

Use `support-copilot.env.example` for sidecar environment wiring and
`live-e2e-checklist.md` for the real Feishu group validation gate.
