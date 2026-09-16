import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";

import OperatorApp from "../src/OperatorApp.vue";

function response(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

const planId = "11111111-1111-1111-1111-111111111111";
const documentId = "22222222-2222-2222-2222-222222222222";
const dashboard = {
  operator: { github_user_id: 42, github_login: "renamed-account" },
  pending_story_count: 3,
  scheduler: { state: "waiting", last_result: "succeeded", updated_at: "2026-09-16T12:00:00Z" },
  sources: [
    { id: "source-1", name: "github-trending", publisher: "GitHub", enabled: true, health: "healthy", recent_result: "succeeded", consecutive_failures: 0, pending_drafts: 2, updated_at: "2026-09-16T12:00:00Z" },
  ],
  plans: [
    { id: planId, publication_date: "2026-09-16", version: 2, included_story_count: 1, excluded_story_count: 1, held_story_count: 0, warning_count: 1, blocker_count: 0, completion: { kind: "published", completed_at: "2026-09-16T13:00:00Z" }, index_follow_up: { state: "succeeded", attempt_count: 1 } },
  ],
};
const detail = {
  ...dashboard.plans[0],
  digest_summary: "Today summary",
  warnings: [{ code: "publisher-diversity", message: "Only one Publisher" }],
  blockers: [],
  stories: [
    {
      id: "story-1",
      stable_key: "story-one",
      headline: "Story one",
      publisher: "GitHub",
      source_url: "https://github.com/example",
      inclusion: "included",
      summary: "Processed summary",
      why_it_matters: "Processed impact",
      primary_topic: "Research",
      secondary_topics: [],
      primary_document_version_id: documentId,
      claims: [
        {
          id: "claim-1",
          text: "A supported claim",
          evidence: [
            { id: "evidence-1", document_version_id: documentId, exact_text: "Exact evidence", role: "primary", relation: "supports", publisher: "GitHub", source_url: "https://github.com/example" },
          ],
        },
      ],
    },
  ],
};

function fetchFixture(input, init = {}) {
  const url = String(input);
  if (url === "/api/operator/session") {
    return Promise.resolve(response({ csrf_token: "csrf-fixture" }));
  }
  if (url === "/api/operator/dashboard") return Promise.resolve(response(dashboard));
  if (url === `/api/operator/plans/${planId}`) return Promise.resolve(response(detail));
  if (url === `/api/operator/document-versions/${documentId}`) {
    return Promise.resolve(response({
      id: documentId,
      title: "Raw source",
      source_url: null,
      body: "<img src=x onerror=alert(1)><script>alert('raw')</script>",
    }));
  }
  if (url === "/operator/logout" && init.method === "POST") {
    return Promise.resolve(response(null, 204));
  }
  return Promise.resolve(response({ detail: "unexpected" }, 500));
}

describe("OperatorApp", () => {
  it("shows Dashboard, Plan history, Evidence, and raw text without executing markup", async () => {
    const wrapper = mount(OperatorApp, { props: { fetchImpl: fetchFixture } });
    await flushPromises();

    expect(wrapper.text()).toContain("renamed-account");
    expect(wrapper.text()).toContain("待处理 Story 3");
    expect(wrapper.text()).toContain("github-trending");
    expect(wrapper.text()).toContain("2026-09-16 · v2");

    await wrapper.get(`[data-plan-id="${planId}"]`).trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("Story one");
    expect(wrapper.text()).toContain("A supported claim");
    expect(wrapper.text()).toContain("Exact evidence");
    expect(wrapper.text()).toContain("Only one Publisher");
    expect(wrapper.get('blockquote a[href="https://github.com/example"]').text()).toBe("证据来源");

    await wrapper.get(`[data-document-id="${documentId}"]`).trigger("click");
    await flushPromises();
    expect(wrapper.get("[data-raw-document]").text()).toContain("<script>alert('raw')</script>");
    expect(wrapper.find("[data-raw-document] script").exists()).toBe(false);
    expect(wrapper.find("[data-raw-document] img").exists()).toBe(false);
  });

  it("logs out with the server CSRF token and exact browser Origin", async () => {
    const fetchImpl = vi.fn(fetchFixture);
    const wrapper = mount(OperatorApp, { props: { fetchImpl } });
    await flushPromises();

    await wrapper.get('[data-action="logout"]').trigger("click");
    await flushPromises();

    expect(fetchImpl).toHaveBeenCalledWith("/operator/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": "csrf-fixture" },
    });
    expect(wrapper.text()).toContain("已注销");
  });

  it("contains no editorial write controls", async () => {
    const wrapper = mount(OperatorApp, { props: { fetchImpl: fetchFixture } });
    await flushPromises();
    expect(wrapper.text()).not.toMatch(/准备|批准|移除|重试索引/);
  });
});
