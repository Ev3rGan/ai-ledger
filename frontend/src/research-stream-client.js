const DEFAULT_ENDPOINT = "/research/answer";
const RESEARCH_SSE_VERSION = "research-sse-2026-09-02.v2";
const EVENT_NAMES = new Set([
  "status",
  "answer.delta",
  "citation",
  "refusal",
  "error",
  "done",
]);
const STATUS_STATES = new Set([
  "retrieving",
  "retrieval-degraded",
  "evidence-assembled",
  "generating",
  "verifying-citations",
]);
const DONE_STATUSES = new Set(["answered", "refused", "failed"]);
const EVIDENCE_ROLES = new Set(["primary", "independent", "secondary", "community"]);
const EVIDENCE_RELATIONS = new Set(["supports", "contradicts"]);
const TIME_SEMANTICS = new Set([
  "event",
  "source-publication",
  "discovery",
  "editorial",
  "digest-publication",
]);

/**
 * @typedef {object} ResearchCitationSupport
 * @property {number} statement_index
 * @property {string | null} dimension
 * @property {string | null} time_semantic
 */

/**
 * @typedef {object} ResearchCitation
 * @property {string} story_id
 * @property {string} story_title
 * @property {string} story_url
 * @property {string} claim_id
 * @property {string} claim_text
 * @property {string} claim_url
 * @property {string} evidence_span_id
 * @property {string} evidence_text
 * @property {string} evidence_url
 * @property {number[]} statement_indexes
 * @property {ResearchCitationSupport[]} statement_support
 * @property {string | null} evidence_role
 * @property {string | null} evidence_relation
 * @property {string | null} evidence_publisher
 * @property {Record<string, string | null>} times
 */

/**
 * @typedef {object} ResearchSnapshot
 * @property {"idle" | "connecting" | "streaming" | "answered" | "refused" | "failed" | "cancelled"} phase
 * @property {string} question
 * @property {string | null} progress
 * @property {string} answer
 * @property {ResearchCitation[]} citations
 * @property {string | null} message
 * @property {string | null} errorCode
 * @property {string | null} terminalStatus
 * @property {boolean} retrievalDegraded
 * @property {string | null} retrievalFallback
 */

export class ResearchProtocolError extends Error {
  constructor(message) {
    super(message);
    this.name = "ResearchProtocolError";
  }
}

/**
 * Owns one Research request at a time, including transport, SSE framing,
 * protocol validation, cancellation races, and terminal state.
 */
export class ResearchStreamClient {
  /**
   * @param {{
   *   fetchImpl?: typeof fetch,
   *   endpoint?: string,
   *   expectedVersion?: string,
   * }} options
   */
  constructor({
    fetchImpl = globalThis.fetch,
    endpoint = DEFAULT_ENDPOINT,
    expectedVersion = RESEARCH_SSE_VERSION,
  } = {}) {
    if (typeof fetchImpl !== "function") {
      throw new TypeError("ResearchStreamClient requires fetch");
    }
    this._fetch = (input, init) => fetchImpl(input, init);
    this._endpoint = endpoint;
    this._expectedVersion = expectedVersion;
    this._listeners = new Set();
    this._requestId = 0;
    this._controller = null;
    this._snapshot = initialSnapshot();
  }

  get snapshot() {
    return cloneSnapshot(this._snapshot);
  }

  /** @param {(snapshot: ResearchSnapshot) => void} listener */
  subscribe(listener) {
    this._listeners.add(listener);
    listener(this.snapshot);
    return () => this._listeners.delete(listener);
  }

  /** @param {string} question */
  async start(question) {
    const normalizedQuestion = question.trim();
    if (!normalizedQuestion) throw new TypeError("Research question is required");

    const requestId = ++this._requestId;
    this._controller?.abort("superseded");
    const controller = new AbortController();
    this._controller = controller;
    const context = { done: false, refusal: false, error: false };
    this._replace({
      ...initialSnapshot(),
      phase: "connecting",
      question: normalizedQuestion,
    });

    try {
      const response = await this._fetch(this._endpoint, {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ question: normalizedQuestion }),
        signal: controller.signal,
      });
      if (requestId !== this._requestId) return this.snapshot;
      if (!response.ok || !response.body) {
        throw new Error(`Research request failed with status ${response.status}`);
      }
      const contentType = response.headers.get("content-type") || "";
      if (!contentType.toLowerCase().includes("text/event-stream")) {
        throw new ResearchProtocolError("Research response is not an SSE stream");
      }
      this._patch({ phase: "streaming" });

      const parser = new SseParser((event, payload) => {
        if (requestId !== this._requestId) return;
        this._acceptEvent(event, payload, context);
      });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { value, done } = await reader.read();
        if (requestId !== this._requestId) {
          await reader.cancel("superseded");
          return this.snapshot;
        }
        if (done) break;
        parser.push(decoder.decode(value, { stream: true }));
      }
      parser.push(decoder.decode());
      parser.finish();
      if (!context.done) {
        throw new ResearchProtocolError("Research stream closed before done");
      }
      return this.snapshot;
    } catch (error) {
      if (requestId !== this._requestId) return this.snapshot;
      if (controller.signal.aborted || isAbortError(error)) {
        this._patch({
          phase: "cancelled",
          terminalStatus: "cancelled",
          progress: null,
        });
        return this.snapshot;
      }
      const protocolFailure = error instanceof ResearchProtocolError;
      this._patch({
        phase: "failed",
        terminalStatus: "failed",
        progress: null,
        errorCode: protocolFailure ? "protocol-error" : "request-error",
        message: protocolFailure
          ? "Research 返回了无法验证的响应。"
          : "Research 服务当前不可用。",
      });
      throw error;
    } finally {
      if (requestId === this._requestId) this._controller = null;
    }
  }

  cancel() {
    if (this._controller === null) return;
    const controller = this._controller;
    this._requestId += 1;
    this._controller = null;
    controller.abort("cancelled");
    this._patch({
      phase: "cancelled",
      terminalStatus: "cancelled",
      progress: null,
    });
  }

  _acceptEvent(event, payload, context) {
    if (context.done) {
      throw new ResearchProtocolError(`Research event received after done: ${event}`);
    }
    validateEvent(event, payload, this._expectedVersion);

    if (event === "status") {
      const retrievalDegraded =
        this._snapshot.retrievalDegraded ||
        payload.state === "retrieval-degraded" ||
        payload.retrieval_degraded === true;
      const retrievalFallback =
        payload.fallback ??
        payload.retrieval_fallback ??
        this._snapshot.retrievalFallback;
      this._patch({
        progress: payload.state,
        retrievalDegraded,
        retrievalFallback,
      });
      return;
    }
    if (event === "answer.delta") {
      this._patch({ answer: this._snapshot.answer + payload.text });
      return;
    }
    if (event === "citation") {
      this._patch({ citations: [...this._snapshot.citations, payload] });
      return;
    }
    if (event === "refusal") {
      context.refusal = true;
      this._patch({ message: payload.message });
      return;
    }
    if (event === "error") {
      context.error = true;
      this._patch({ message: payload.message, errorCode: payload.code });
      return;
    }

    context.done = true;
    if (context.refusal && payload.status !== "refused") {
      throw new ResearchProtocolError("Research refusal has an inconsistent done status");
    }
    if (context.error && payload.status !== "failed") {
      throw new ResearchProtocolError("Research error has an inconsistent done status");
    }
    this._patch({
      phase: payload.status,
      terminalStatus: payload.status,
      progress: null,
    });
  }

  _replace(snapshot) {
    this._snapshot = snapshot;
    this._emit();
  }

  _patch(fields) {
    this._snapshot = { ...this._snapshot, ...fields };
    this._emit();
  }

  _emit() {
    const snapshot = this.snapshot;
    for (const listener of this._listeners) listener(snapshot);
  }
}

class SseParser {
  /** @param {(event: string, payload: object) => void} onEvent */
  constructor(onEvent) {
    this._onEvent = onEvent;
    this._buffer = "";
  }

  /** @param {string} text */
  push(text) {
    this._buffer += text;
    while (true) {
      const match = /\r\n\r\n|\n\n|\r\r/.exec(this._buffer);
      if (match === null) return;
      const block = this._buffer.slice(0, match.index);
      this._buffer = this._buffer.slice(match.index + match[0].length);
      if (block.trim()) this._dispatch(block);
    }
  }

  finish() {
    if (this._buffer.trim()) {
      throw new ResearchProtocolError("Research stream ended with an incomplete SSE event");
    }
  }

  /** @param {string} block */
  _dispatch(block) {
    let event = "";
    const data = [];
    for (const line of block.split(/\r\n|\r|\n/)) {
      if (!line || line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator < 0 ? line : line.slice(0, separator);
      let value = separator < 0 ? "" : line.slice(separator + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "event") event = value;
      if (field === "data") data.push(value);
    }
    if (!event || data.length === 0) {
      throw new ResearchProtocolError("Research SSE event is missing event or data");
    }
    let payload;
    try {
      payload = JSON.parse(data.join("\n"));
    } catch (error) {
      throw new ResearchProtocolError("Research SSE data is not valid JSON", { cause: error });
    }
    this._onEvent(event, payload);
  }
}

/** @returns {ResearchSnapshot} */
function initialSnapshot() {
  return {
    phase: "idle",
    question: "",
    progress: null,
    answer: "",
    citations: [],
    message: null,
    errorCode: null,
    terminalStatus: null,
    retrievalDegraded: false,
    retrievalFallback: null,
  };
}

function cloneSnapshot(snapshot) {
  return { ...snapshot, citations: [...snapshot.citations] };
}

function isAbortError(error) {
  return error instanceof DOMException && error.name === "AbortError";
}

function validateEvent(event, payload, expectedVersion) {
  if (!EVENT_NAMES.has(event)) {
    throw new ResearchProtocolError(`Unknown Research event: ${event}`);
  }
  requireObject(payload, `${event} payload`);
  requireString(payload, "version", event);
  if (payload.version !== expectedVersion) {
    throw new ResearchProtocolError(`Unexpected Research contract version: ${payload.version}`);
  }
  if (event === "status") {
    requireString(payload, "state", event);
    if (!STATUS_STATES.has(payload.state)) {
      throw new ResearchProtocolError(`Unknown Research status state: ${payload.state}`);
    }
    validateStatusDetails(payload);
    return;
  }
  if (event === "answer.delta") {
    requireString(payload, "text", event);
    return;
  }
  if (event === "citation") {
    for (const field of [
      "story_id",
      "story_title",
      "story_url",
      "claim_id",
      "claim_text",
      "claim_url",
      "evidence_span_id",
      "evidence_text",
      "evidence_url",
    ]) {
      requireString(payload, field, event);
    }
    requireArray(payload, "statement_indexes", event);
    requireArray(payload, "statement_support", event);
    requireObject(payload.times, "citation times");
    for (const index of payload.statement_indexes) {
      requirePositiveInteger(index, "citation statement index");
    }
    for (const support of payload.statement_support) {
      requireObject(support, "citation statement support");
      requirePositiveInteger(support.statement_index, "citation support statement_index");
      requireNullableString(support.dimension, "citation support dimension");
      requireNullableString(support.time_semantic, "citation support time_semantic");
      if (support.time_semantic !== null && !TIME_SEMANTICS.has(support.time_semantic)) {
        throw new ResearchProtocolError(
          `Unknown citation time semantic: ${support.time_semantic}`,
        );
      }
    }
    requireNullableEnum(payload.evidence_role, EVIDENCE_ROLES, "citation evidence_role");
    requireNullableEnum(
      payload.evidence_relation,
      EVIDENCE_RELATIONS,
      "citation evidence_relation",
    );
    requireNullableString(payload.evidence_publisher, "citation evidence_publisher");
    for (const value of Object.values(payload.times)) {
      requireNullableString(value, "citation time value");
    }
    return;
  }
  if (event === "refusal") {
    requireString(payload, "message", event);
    if (typeof payload.reason !== "string" && typeof payload.code !== "string") {
      throw new ResearchProtocolError("refusal payload needs reason or code");
    }
    return;
  }
  if (event === "error") {
    requireString(payload, "code", event);
    requireString(payload, "message", event);
    return;
  }
  requireString(payload, "status", event);
  if (!DONE_STATUSES.has(payload.status)) {
    throw new ResearchProtocolError(`Unknown Research done status: ${payload.status}`);
  }
}

function requireObject(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new ResearchProtocolError(`${label} must be an object`);
  }
}

function requireString(payload, field, event) {
  if (typeof payload[field] !== "string") {
    throw new ResearchProtocolError(`${event} payload field ${field} must be a string`);
  }
}

function requireArray(payload, field, event) {
  if (!Array.isArray(payload[field])) {
    throw new ResearchProtocolError(`${event} payload field ${field} must be an array`);
  }
}

function validateStatusDetails(payload) {
  for (const field of ["fallback", "retrieval_fallback"]) {
    if (field in payload) requireNullableString(payload[field], `status ${field}`);
  }
  if (
    "retrieval_degraded" in payload &&
    typeof payload.retrieval_degraded !== "boolean"
  ) {
    throw new ResearchProtocolError("status retrieval_degraded must be a boolean");
  }
  for (const field of ["faults", "retrieval_faults"]) {
    if (!(field in payload)) continue;
    if (!Array.isArray(payload[field])) {
      throw new ResearchProtocolError(`status ${field} must be an array`);
    }
    for (const fault of payload[field]) {
      requireObject(fault, `status ${field} entry`);
      requireString(fault, "stage", `status ${field} entry`);
      requireString(fault, "code", `status ${field} entry`);
    }
  }
}

function requirePositiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new ResearchProtocolError(`${label} must be a positive integer`);
  }
}

function requireNullableString(value, label) {
  if (value !== null && typeof value !== "string") {
    throw new ResearchProtocolError(`${label} must be a string or null`);
  }
}

function requireNullableEnum(value, allowed, label) {
  requireNullableString(value, label);
  if (value !== null && !allowed.has(value)) {
    throw new ResearchProtocolError(`Unknown ${label}: ${value}`);
  }
}
