#!/usr/bin/env node

const endpoint =
  process.env.SUPPORT_COPILOT_URL ||
  "http://127.0.0.1:8000/channels/openclaw-feishu/support-case";
const secret = process.env.OPENCLAW_FEISHU_BRIDGE_SECRET || "";

const payload = {
  contractOnly: true,
  batchId: "smoke-openclaw-feishu-001",
  messages: [
    {
      chatId: "oc_smoke_chat",
      chatType: "group",
      messageId: "om_smoke_text",
      threadId: "omt_smoke_thread",
      senderId: "ou_smoke_sender",
      content: "客户反馈 L023 不亮，补充了一张疑似损坏图片。",
      contentType: "text",
      resources: [],
    },
    {
      chatId: "oc_smoke_chat",
      chatType: "group",
      messageId: "om_smoke_image",
      threadId: "omt_smoke_thread",
      senderId: "ou_smoke_sender",
      content: "",
      contentType: "image",
      resources: [
        {
          type: "image",
          imageKey: "img_smoke_damage",
          fileName: "damage.jpg",
          mimeType: "image/jpeg",
          status: "error",
          downloadError: "smoke: no real Feishu download in local contract check",
          description: "产品损坏照片",
        },
      ],
    },
  ],
};

const headers = {
  "content-type": "application/json",
};
if (secret) {
  headers["x-openclaw-feishu-secret"] = secret;
}

const response = await fetch(endpoint, {
  method: "POST",
  headers,
  body: JSON.stringify(payload),
});

const responseText = await response.text();
let responseJson;
try {
  responseJson = responseText ? JSON.parse(responseText) : {};
} catch (error) {
  fail(`Support Copilot returned non-JSON response: ${error.message}`, responseText);
}

if (!response.ok) {
  fail(`Support Copilot returned HTTP ${response.status}`, responseJson);
}

assertEquals(responseJson.channel, "feishu", "channel");
assertEquals(responseJson.mode, "thread_reply", "mode");
assertEquals(responseJson.replyInThread, true, "replyInThread");
assertEquals(responseJson.chatId, "oc_smoke_chat", "chatId");
assertEquals(responseJson.threadId, "omt_smoke_thread", "threadId");
assertEquals(responseJson.replyToMessageId, "om_smoke_image", "replyToMessageId");
assertNonEmptyString(responseJson.text, "text");
assertNonEmptyString(responseJson.fallbackText, "fallbackText");

console.log(
  JSON.stringify(
    {
      ok: true,
      endpoint,
      mode: responseJson.mode,
      replyInThread: responseJson.replyInThread,
      replyToMessageId: responseJson.replyToMessageId,
      recommendedAction: responseJson.metadata?.recommendedAction || "",
      textPreview: responseJson.fallbackText.slice(0, 160),
    },
    null,
    2,
  ),
);

function assertEquals(actual, expected, field) {
  if (actual !== expected) {
    fail(`Expected ${field}=${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`, responseJson);
  }
}

function assertNonEmptyString(value, field) {
  if (typeof value !== "string" || value.trim() === "") {
    fail(`Expected ${field} to be a non-empty string`, responseJson);
  }
}

function fail(message, details) {
  console.error(message);
  if (details !== undefined) {
    console.error(typeof details === "string" ? details : JSON.stringify(details, null, 2));
  }
  process.exit(1);
}
