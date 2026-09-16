import { mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";

import ResearchApp from "../src/ResearchApp.vue";

function snapshot(overrides = {}) {
  return {
    phase: "idle",
    question: "",
    progress: null,
    answer: "",
    citations: [],
    message: null,
    errorCode: null,
    terminalStatus: null,
    ...overrides,
  };
}

class FakeResearchClient {
  constructor() {
    this.current = snapshot();
    this.listener = null;
    this.start = vi.fn(async () => this.current);
    this.cancel = vi.fn();
  }

  subscribe(listener) {
    this.listener = listener;
    listener(this.current);
    return () => {
      this.listener = null;
    };
  }

  emit(fields) {
    this.current = snapshot({ ...this.current, ...fields });
    this.listener(this.current);
  }
}

describe("ResearchApp", () => {
  it("offers semantic form controls and lets a keyboard-focusable example fill the question", async () => {
    const client = new FakeResearchClient();
    const wrapper = mount(ResearchApp, {
      props: { client, examples: ["哪些事实有公开依据？"] },
      attachTo: document.body,
    });

    const textarea = wrapper.get('textarea[name="question"]');
    expect(wrapper.get('label[for="research-question"]').text()).toBe("你的问题");
    expect(wrapper.get('button[type="submit"]').text()).toContain("查找答案");
    const example = wrapper.get("button.research-example");
    expect(example.attributes("type")).toBe("button");
    await example.trigger("keydown", { key: "Enter" });

    expect(textarea.element.value).toBe("哪些事实有公开依据？");
    expect(document.activeElement).toBe(textarea.element);
    wrapper.unmount();
  });

  it("renders progress, incremental answer, citations, and cancellation from client state", async () => {
    const client = new FakeResearchClient();
    const wrapper = mount(ResearchApp, { props: { client, examples: [] } });
    await wrapper.get('textarea[name="question"]').setValue("请给出依据");
    await wrapper.get("form").trigger("submit");

    expect(client.start).toHaveBeenCalledWith("请给出依据");
    client.emit({ phase: "streaming", progress: "generating", question: "请给出依据" });
    await nextTick();
    expect(wrapper.get('[role="status"]').text()).toContain("正在生成");
    await wrapper.get("button[data-action=cancel]").trigger("click");
    expect(client.cancel).toHaveBeenCalledOnce();

    client.emit({
      phase: "answered",
      progress: null,
      answer: "一个有依据的答案。",
      citations: [
        {
          evidence_url: "/stories/story-1#evidence-evidence-1",
          story_title: "已发布 Story",
          claim_text: "受支持 Claim",
          evidence_text: "公开 Evidence 摘要",
          evidence_role: "primary",
          statement_support: [{ statement_index: 1, time_semantic: null }],
        },
      ],
    });
    await nextTick();

    expect(wrapper.text()).toContain("一个有依据的答案。");
    expect(wrapper.get(".research-citations a").attributes("href")).toBe(
      "/stories/story-1#evidence-evidence-1",
    );
    expect(wrapper.get('[role="status"]').text()).toContain("回答完成");
  });

  it("keeps retrieval degradation visible while later progress is rendered", async () => {
    const client = new FakeResearchClient();
    const wrapper = mount(ResearchApp, { props: { client, examples: [] } });

    client.emit({
      phase: "streaming",
      progress: "generating",
      retrievalDegraded: true,
      retrievalFallback: "lexical",
    });
    await nextTick();

    expect(wrapper.get('[role="status"]').text()).toContain("检索降级");
    expect(wrapper.get('[role="status"]').text()).toContain("lexical");
    expect(wrapper.get('[role="status"]').text()).toContain("正在生成");
  });

  it.each([
    ["refused", "证据不足，无法回答。", "已拒答"],
    ["failed", "Research 服务当前不可用。", "请求失败"],
  ])("renders %s and retries the submitted question", async (phase, message, label) => {
    const client = new FakeResearchClient();
    const wrapper = mount(ResearchApp, { props: { client, examples: [] } });
    await wrapper.get('textarea[name="question"]').setValue("需要重试的问题");
    client.emit({ phase, question: "需要重试的问题", message, terminalStatus: phase });
    await nextTick();

    expect(wrapper.get('[role="alert"]').text()).toContain(message);
    expect(wrapper.get('[role="status"]').text()).toContain(label);
    await wrapper.get("button[data-action=retry]").trigger("click");

    expect(client.start).toHaveBeenCalledWith("需要重试的问题");
  });
});
