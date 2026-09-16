import { describe, expect, it, vi } from "vitest";

import {
  ResearchProtocolError,
  ResearchStreamClient,
} from "../src/research-stream-client.js";

const VERSION = "research-sse-2026-09-02.v2";

function sse(event, payload, newline = "\n") {
  return `event: ${event}${newline}data: ${JSON.stringify(payload)}${newline}${newline}`;
}

function responseFromBytes(bytes, splitPoints = []) {
  const chunks = [];
  let start = 0;
  for (const end of [...splitPoints, bytes.length]) {
    chunks.push(bytes.slice(start, end));
    start = end;
  }
  return new Response(
    new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(chunk);
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" } },
  );
}

function responseFromText(text, splitPoints = []) {
  return responseFromBytes(new TextEncoder().encode(text), splitPoints);
}

function answeredStream(answer = "有依据的答案") {
  return responseFromText(
    sse("answer.delta", { version: VERSION, text: answer }) +
      sse("done", { version: VERSION, status: "answered" }),
  );
}

function citationPayload(overrides = {}) {
  return {
    version: VERSION,
    story_id: "story-1",
    story_title: "已发布 Story",
    story_url: "/stories/story-1",
    claim_id: "claim-1",
    claim_text: "一个受支持的事实",
    claim_url: "/stories/story-1#claim-claim-1",
    evidence_span_id: "evidence-1",
    evidence_text: "公开的有界 Evidence 摘要",
    evidence_url: "/stories/story-1#evidence-evidence-1",
    statement_indexes: [1],
    statement_support: [{ statement_index: 1, dimension: null, time_semantic: null }],
    evidence_role: "primary",
    evidence_relation: "supports",
    evidence_publisher: "示例发布者",
    times: {},
    ...overrides,
  };
}

describe("ResearchStreamClient", () => {
  it("invokes fetch without rebinding the native function receiver", async () => {
    const fetchImpl = vi.fn(function fetchWithReceiverCheck() {
      expect(this).toBeUndefined();
      return Promise.resolve(answeredStream());
    });
    const client = new ResearchStreamClient({ fetchImpl });

    await client.start("浏览器原生 fetch 调用边界");

    expect(client.snapshot.phase).toBe("answered");
  });

  it("parses arbitrary byte boundaries and multiple events from one chunk", async () => {
    const citation = citationPayload();
    const body =
      sse("status", { version: VERSION, state: "retrieving" }, "\r\n") +
      sse("answer.delta", { version: VERSION, text: "有依据" }) +
      sse("answer.delta", { version: VERSION, text: "的答案" }) +
      sse("citation", citation) +
      sse("done", { version: VERSION, status: "answered" });
    const fetchImpl = vi.fn(async () =>
      responseFromText(body, [1, 2, 7, 19, 61, 62, 133]),
    );
    const client = new ResearchStreamClient({ fetchImpl });

    const result = await client.start("这个问题有什么依据？");

    expect(fetchImpl).toHaveBeenCalledOnce();
    expect(result.phase).toBe("answered");
    expect(result.answer).toBe("有依据的答案");
    expect(result.citations).toEqual([citation]);
    expect(result.terminalStatus).toBe("answered");
  });

  it("preserves retrieval degradation through later progress events", async () => {
    const body =
      sse("status", {
        version: VERSION,
        state: "retrieval-degraded",
        fallback: "lexical",
        faults: [{ stage: "vector", code: "unavailable" }],
      }) +
      sse("status", {
        version: VERSION,
        state: "generating",
        retrieval_degraded: true,
        retrieval_fallback: "lexical",
        retrieval_faults: [{ stage: "vector", code: "unavailable" }],
      }) +
      sse("answer.delta", { version: VERSION, text: "降级后的有据答案" }) +
      sse("done", { version: VERSION, status: "answered" });
    const client = new ResearchStreamClient({
      fetchImpl: async () => responseFromText(body),
    });

    const result = await client.start("检索降级时发生什么？");

    expect(result.retrievalDegraded).toBe(true);
    expect(result.retrievalFallback).toBe("lexical");
  });

  it.each([
    ["non-integer statement index", { statement_indexes: ["1"] }],
    [
      "malformed statement support",
      { statement_support: [{ statement_index: "1", dimension: null, time_semantic: null }] },
    ],
    ["malformed time value", { times: { event: 123 } }],
    ["malformed evidence role", { evidence_role: false }],
  ])("rejects citation payloads with %s", async (_label, overrides) => {
    const client = new ResearchStreamClient({
      fetchImpl: async () =>
        responseFromText(
          sse("citation", citationPayload(overrides)) +
            sse("done", { version: VERSION, status: "answered" }),
        ),
    });

    await expect(client.start("测试 citation schema")).rejects.toBeInstanceOf(
      ResearchProtocolError,
    );
    expect(client.snapshot.errorCode).toBe("protocol-error");
  });

  it("preserves the JSON parse error as the protocol error cause", async () => {
    const client = new ResearchStreamClient({
      fetchImpl: async () =>
        responseFromText("event: status\ndata: {not-json}\n\n"),
    });

    let failure;
    try {
      await client.start("测试异常链");
    } catch (error) {
      failure = error;
    }

    expect(failure).toBeInstanceOf(ResearchProtocolError);
    expect(failure.cause).toBeInstanceOf(SyntaxError);
  });

  it.each([
    ["unknown event", sse("mystery", { version: VERSION })],
    ["malformed JSON", "event: status\ndata: {not-json}\n\n"],
    ["malformed event schema", sse("status", { version: VERSION })],
    ["wrong contract version", sse("status", { version: "old-version", state: "retrieving" })],
  ])("fails closed for %s", async (_label, body) => {
    const client = new ResearchStreamClient({
      fetchImpl: async () => responseFromText(body),
    });

    await expect(client.start("测试协议失败")).rejects.toBeInstanceOf(
      ResearchProtocolError,
    );
    expect(client.snapshot.phase).toBe("failed");
    expect(client.snapshot.errorCode).toBe("protocol-error");
  });

  it("fails when the stream closes without one done event", async () => {
    const client = new ResearchStreamClient({
      fetchImpl: async () =>
        responseFromText(sse("status", { version: VERSION, state: "retrieving" })),
    });

    await expect(client.start("测试不完整流")).rejects.toThrow("before done");
    expect(client.snapshot.phase).toBe("failed");
  });

  it("rejects duplicate terminal signals", async () => {
    const client = new ResearchStreamClient({
      fetchImpl: async () =>
        responseFromText(
          sse("done", { version: VERSION, status: "answered" }) +
            sse("done", { version: VERSION, status: "answered" }),
        ),
    });

    await expect(client.start("测试重复终止")).rejects.toThrow("after done");
    expect(client.snapshot.phase).toBe("failed");
  });

  it("keeps a newer request authoritative when an older cancellation settles later", async () => {
    let firstSignal;
    const fetchImpl = vi.fn(async (_url, options) => {
      const question = JSON.parse(options.body).question;
      if (question === "旧问题") {
        firstSignal = options.signal;
        return new Response(
          new ReadableStream({
            start(controller) {
              options.signal.addEventListener("abort", () => {
                queueMicrotask(() => controller.error(new DOMException("Aborted", "AbortError")));
              });
            },
          }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        );
      }
      return answeredStream("新答案");
    });
    const client = new ResearchStreamClient({ fetchImpl });

    const oldRequest = client.start("旧问题");
    const newRequest = client.start("新问题");
    await Promise.allSettled([oldRequest, newRequest]);

    expect(firstSignal.aborted).toBe(true);
    expect(client.snapshot.phase).toBe("answered");
    expect(client.snapshot.answer).toBe("新答案");
  });

  it("exposes an explicit cancelled terminal state", async () => {
    let requestStarted;
    const started = new Promise((resolve) => {
      requestStarted = resolve;
    });
    const client = new ResearchStreamClient({
      fetchImpl: async (_url, options) =>
        new Response(
          new ReadableStream({
            start(controller) {
              options.signal.addEventListener("abort", () => {
                controller.error(new DOMException("Aborted", "AbortError"));
              });
              requestStarted();
            },
          }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        ),
    });

    const request = client.start("取消这个问题");
    await started;
    client.cancel();
    await request;

    expect(client.snapshot.phase).toBe("cancelled");
    expect(client.snapshot.terminalStatus).toBe("cancelled");
  });

  it("keeps cancellation terminal when the transport ignores abort", async () => {
    let resolveResponse;
    const response = new Promise((resolve) => {
      resolveResponse = resolve;
    });
    const client = new ResearchStreamClient({
      fetchImpl: () => response,
    });

    const request = client.start("取消后不可复活");
    client.cancel();
    resolveResponse(answeredStream("不应出现的答案"));
    await request;

    expect(client.snapshot.phase).toBe("cancelled");
    expect(client.snapshot.answer).toBe("");
  });
});
