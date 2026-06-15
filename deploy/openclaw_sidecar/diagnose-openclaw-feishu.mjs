#!/usr/bin/env node

import { spawnSync } from "node:child_process";

const supportCopilotHealthUrl =
  process.env.SUPPORT_COPILOT_HEALTH_URL ||
  deriveHealthUrl(
    process.env.SUPPORT_COPILOT_URL ||
      "http://127.0.0.1:8000/channels/openclaw-feishu/support-case",
  );
const allowUnconfigured = process.argv.includes("--allow-unconfigured");

const checks = [];

checks.push(runCommand("openclaw-version", "openclaw", ["--version"], { required: true }));
checks.push(
  runCommand("openclaw-lark-cli-version", "openclaw-lark", ["--tools-version", "2026.6.10", "--cli-version"], {
    required: true,
  }),
);
checks.push(runCommand("openclaw-config-validate", "openclaw", ["config", "validate"], { required: !allowUnconfigured }));
checks.push(runCommand("openclaw-plugins-list", "openclaw", ["plugins", "list"], { required: !allowUnconfigured }));
checks.push(
  runCommand(
    "openclaw-feishu-channel-status",
    "openclaw",
    ["channels", "status", "--channel", "feishu", "--json", "--timeout", "10000"],
    { required: !allowUnconfigured },
  ),
);
checks.push(await checkSupportCopilotHealth(supportCopilotHealthUrl));

const failedRequired = checks.filter((check) => check.required && !check.ok);
console.log(
  JSON.stringify(
    {
      ok: failedRequired.length === 0,
      allowUnconfigured,
      supportCopilotHealthUrl,
      checks,
    },
    null,
    2,
  ),
);

if (failedRequired.length > 0) {
  process.exit(1);
}

function runCommand(name, command, args, { required }) {
  const result = spawnSync(command, args, {
    encoding: "utf-8",
    shell: false,
  });
  const stdout = (result.stdout || "").trim();
  const stderr = (result.stderr || "").trim();
  return {
    name,
    required,
    ok: result.status === 0,
    command: [command, ...args].join(" "),
    exitCode: result.status,
    stdout: stdout.slice(0, 4000),
    stderr: stderr.slice(0, 4000),
    error: result.error ? result.error.message : "",
  };
}

async function checkSupportCopilotHealth(url) {
  try {
    const response = await fetch(url, { method: "GET" });
    const text = await response.text();
    let body = {};
    try {
      body = text ? JSON.parse(text) : {};
    } catch (error) {
      return {
        name: "support-copilot-health",
        required: true,
        ok: false,
        url,
        status: response.status,
        error: `non-JSON health response: ${error.message}`,
        bodyPreview: text.slice(0, 4000),
      };
    }
    return {
      name: "support-copilot-health",
      required: true,
      ok:
        response.ok &&
        body.ok === true &&
        body.channel === "openclaw_feishu" &&
        body.runtime === "support_copilot",
      url,
      status: response.status,
      body,
    };
  } catch (error) {
    return {
      name: "support-copilot-health",
      required: true,
      ok: false,
      url,
      error: error.message,
    };
  }
}

function deriveHealthUrl(supportCaseUrl) {
  return supportCaseUrl.replace(/\/support-case\/?$/, "/health");
}
