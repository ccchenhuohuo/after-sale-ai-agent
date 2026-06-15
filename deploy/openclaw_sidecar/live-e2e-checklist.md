# OpenClaw Feishu Live E2E Checklist

Use this checklist when validating the real Feishu group flow. The local smoke
script proves only the HTTP contract; it does not prove Feishu credentials,
OpenClaw gateway delivery, file download permissions, or thread replies in a
real group.

## Preconditions

- Python Support Copilot service is running.
- `OPENCLAW_FEISHU_BRIDGE_SECRET` is configured identically in the Python
  service and the OpenClaw sidecar environment.
- OpenClaw sidecar uses `openclaw@2026.6.6` and
  `@larksuite/openclaw-lark@2026.6.10`.
- Feishu app credentials, encrypt key, verification token, permissions, bot
  membership, and target test group are configured in OpenClaw Lark plugin
  config.
- Legacy `feishu-long-connection` is either stopped or pointed at a different
  test group to avoid duplicate replies during this validation.

## Local Service Checks

```bash
curl -s http://127.0.0.1:8000/channels/openclaw-feishu/health
```

Expected shape:

```json
{"ok":true,"channel":"openclaw_feishu","runtime":"support_copilot","requiresSecret":true}
```

Then run the local contract smoke:

```bash
cd deploy/openclaw_sidecar
nvm use 22.22.2
corepack npm run doctor:support-copilot
corepack npm run smoke:support-copilot
```

## Feishu Group Checks

- Send one text-only after-sales question in the target thread.
- Send one text message plus one image in quick succession in the same thread.
- Send one image or video resource with no text.
- Confirm the sidecar forwards a single `SupportCaseRequest` per burst when
  messages are grouped.
- Confirm the Python runtime returns a payload with `mode=thread_reply` and
  `replyInThread=true`.
- Confirm OpenClaw replies only inside the original thread.
- Confirm failed asset download is visible as missing/failed evidence and does
  not cause the whole reply to fail.
- Confirm the visible reply does not expose internal schema names, traces,
  tool names, raw vectors, file keys, local paths, or URLs.
- Confirm `human_review` remains a suggestion only and does not actually
  mention a responsible owner.

## Rollback

- Stop the OpenClaw sidecar.
- Re-enable legacy `feishu-long-connection` only if it was stopped.
- Keep the Python `SupportCaseRequest`/`SupportRuntimeResult` runtime unchanged;
  channel rollback should not require Agent/runtime code changes.
