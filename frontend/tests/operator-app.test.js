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
    { id: planId, publication_date: "2026-09-16", version: 2, content_hash: "a".repeat(64), included_story_count: 1, excluded_story_count: 1, held_story_count: 0, warning_count: 1, blocker_count: 0, completion: null, index_follow_up: null },
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
  if (url === `/api/operator/plans/${planId}/stories/story-one/remove` && init.method === "POST") {
    return Promise.resolve(response({
      ...detail,
      id: "33333333-3333-3333-3333-333333333333",
      version: 3,
      content_hash: "b".repeat(64),
      included_story_count: 0,
      excluded_story_count: 2,
      stories: detail.stories.map((story) => ({
        ...story,
        inclusion: "excluded",
        order: null,
        exclusion_reason: "Not suitable for this edition",
      })),
    }, 201));
  }
  if (url === "/api/operator/plans/33333333-3333-3333-3333-333333333333/approve" && init.method === "POST") {
    return Promise.resolve(response({
      kind: "no-publication",
      completed_at: "2026-09-16T13:00:00Z",
      digest_id: null,
      public_url: null,
      follow_up: null,
    }));
  }
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
  it("defaults the preparation date to the Asia/Shanghai calendar day", async () => {
    const wrapper = mount(OperatorApp, {
      props: {
        fetchImpl: fetchFixture,
        nowFactory: () => new Date("2026-09-16T16:30:00Z"),
      },
    });
    await flushPromises();

    expect(wrapper.get("#plan-publication-date").element.value).toBe("2026-09-17");
  });

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

  it("removes one Story from the exact Plan and approves the derived latest Plan", async () => {
    const fetchImpl = vi.fn(fetchFixture);
    const wrapper = mount(OperatorApp, {
      props: {
        fetchImpl,
        idempotencyKeyFactory: vi.fn()
          .mockReturnValueOnce("remove-story-key")
          .mockReturnValueOnce("approve-plan-key"),
      },
    });
    await flushPromises();

    await wrapper.get(`[data-plan-id="${planId}"]`).trigger("click");
    await flushPromises();
    await wrapper.get('[data-removal-reason="story-one"]').setValue(
      "Not suitable for this edition",
    );
    await wrapper.get('[data-action="remove-story-one"]').trigger("click");
    await flushPromises();

    expect(fetchImpl).toHaveBeenCalledWith(
      `/api/operator/plans/${planId}/stories/story-one/remove`,
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "X-CSRF-Token": "csrf-fixture",
          "Idempotency-Key": "remove-story-key",
        }),
        body: JSON.stringify({
          expected_version: 2,
          expected_content_hash: detail.content_hash,
          reason: "Not suitable for this edition",
        }),
      }),
    );
    expect(wrapper.text()).toContain("Plan 检查 · 2026-09-16 v3");

    await wrapper.get('[data-action="approve-plan"]').trigger("click");
    await flushPromises();
    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/operator/plans/33333333-3333-3333-3333-333333333333/approve",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Idempotency-Key": "approve-plan-key",
        }),
        body: JSON.stringify({
          expected_version: 3,
          expected_content_hash: "b".repeat(64),
        }),
      }),
    );
    expect(wrapper.text()).toContain("本日已确认不发布");
    expect(wrapper.find('input[name="headline"]').exists()).toBe(false);
    expect(wrapper.find('[data-action="reorder-story"]').exists()).toBe(false);
  });

  it("completes the A/D removal, B/C publication, and failed follow-up retry journey", async () => {
    const story = (stableKey, order) => ({
      id: `story-${stableKey}`,
      stable_key: stableKey,
      headline: `Story ${stableKey}`,
      publisher: "Fixture",
      source_url: null,
      inclusion: "included",
      order,
      summary: `Summary ${stableKey}`,
      why_it_matters: `Impact ${stableKey}`,
      primary_topic: "Research",
      secondary_topics: [],
      primary_document_version_id: documentId,
      claims: [],
    });
    const ids = {
      first: "aaaaaaaa-1111-1111-1111-111111111111",
      second: "bbbbbbbb-2222-2222-2222-222222222222",
      third: "cccccccc-3333-3333-3333-333333333333",
    };
    const hashes = { first: "1".repeat(64), second: "2".repeat(64), third: "3".repeat(64) };
    const plans = {
      first: { ...detail, id: ids.first, version: 1, content_hash: hashes.first, stories: [story("A", 0), story("B", 1), story("C", 2), story("D", 3)], completion: null, index_follow_up: null },
      second: { ...detail, id: ids.second, version: 2, content_hash: hashes.second, stories: [{ ...story("A", null), inclusion: "excluded", exclusion_reason: "Remove A" }, story("B", 0), story("C", 1), story("D", 2)], completion: null, index_follow_up: null },
      third: { ...detail, id: ids.third, version: 3, content_hash: hashes.third, stories: [{ ...story("A", null), inclusion: "excluded", exclusion_reason: "Remove A" }, story("B", 0), story("C", 1), { ...story("D", null), inclusion: "excluded", exclusion_reason: "Remove D" }], completion: null, index_follow_up: null },
    };
    const journeyDashboard = {
      ...dashboard,
      plans: [{ ...dashboard.plans[0], id: ids.first, version: 1, content_hash: hashes.first }],
    };
    const fetchImpl = vi.fn((input, init = {}) => {
      const url = String(input);
      if (url === "/api/operator/session") return Promise.resolve(response({ csrf_token: "csrf-fixture" }));
      if (url === "/api/operator/dashboard") return Promise.resolve(response(journeyDashboard));
      if (url === `/api/operator/plans/${ids.first}`) return Promise.resolve(response(plans.first));
      if (url.endsWith("/stories/A/remove") && init.method === "POST") return Promise.resolve(response(plans.second, 201));
      if (url.endsWith("/stories/D/remove") && init.method === "POST") return Promise.resolve(response(plans.third, 201));
      if (url === `/api/operator/plans/${ids.third}/approve` && init.method === "POST") {
        return Promise.resolve(response({
          kind: "published",
          completed_at: "2026-09-16T13:00:00Z",
          digest_id: "dddddddd-4444-4444-4444-444444444444",
          public_url: "/digests/2026-09-16",
          follow_up: { state: "failed", attempt_count: 1, last_error: "fixture failure" },
        }));
      }
      if (url === `/api/operator/plans/${ids.third}/follow-up/retry` && init.method === "POST") {
        return Promise.resolve(response({ state: "queued", attempt_count: 1, last_error: null }));
      }
      return Promise.resolve(response({ detail: `unexpected ${url}` }, 500));
    });
    const keys = ["remove-a", "remove-d", "approve-b-c", "retry-index"];
    const wrapper = mount(OperatorApp, {
      props: { fetchImpl, idempotencyKeyFactory: () => keys.shift() },
    });
    await flushPromises();
    await wrapper.get(`[data-plan-id="${ids.first}"]`).trigger("click");
    await flushPromises();

    await wrapper.get('[data-removal-reason="A"]').setValue("Remove A");
    await wrapper.get('[data-action="remove-A"]').trigger("click");
    await flushPromises();
    await wrapper.get('[data-removal-reason="D"]').setValue("Remove D");
    await wrapper.get('[data-action="remove-D"]').trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("included · Research · Fixture");
    expect(wrapper.text()).toContain("excluded · Research · Fixture");
    await wrapper.get('[data-action="approve-plan"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('a[href="/digests/2026-09-16"]').text()).toBe("查看公开日报");
    expect(wrapper.get('[data-follow-up-status]').text()).toContain("failed");

    await wrapper.get('[data-action="retry-follow-up"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-follow-up-status]').text()).toContain("queued");
    expect(keys).toEqual([]);
    expect(wrapper.find('input[name="headline"]').exists()).toBe(false);
    expect(wrapper.find('[data-action="reorder-story"]').exists()).toBe(false);
  });
});
