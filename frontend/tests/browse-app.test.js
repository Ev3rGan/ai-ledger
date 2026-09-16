import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";

import BrowseApp from "../src/BrowseApp.vue";

const initialState = {
  filters: { q: null, source: null, topic: null, date: null },
  facets: { sources: ["示例发布者"], topics: ["Products and Tools"] },
  items: [
    {
      url: "/stories/story-1",
      headline: "已发布 Story",
      summary: "公开摘要",
      publisher: "示例发布者",
      topic: "Products and Tools",
      secondary_topics: ["Research", "Business"],
      published_at: null,
    },
  ],
  pagination: { page: 1, page_size: 12, total_items: 13, total_pages: 2 },
};

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

describe("BrowseApp", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "/browse");
  });

  it("keeps primary and secondary topics visible after Vue mounts", () => {
    const wrapper = mount(BrowseApp, {
      props: { initialState, fetchImpl: vi.fn() },
    });

    expect(wrapper.get(".story-card .topic").text()).toBe(
      "Products and Tools · Research · Business",
    );
  });

  it("shows loading, synchronizes the URL, and renders an empty result", async () => {
    const pending = deferred();
    const fetchImpl = vi.fn(() => pending.promise);
    const wrapper = mount(BrowseApp, {
      props: { initialState, fetchImpl },
    });

    await wrapper.get('input[name="q"]').setValue("没有结果");
    wrapper.get("form").element.dispatchEvent(new Event("submit", { bubbles: true }));
    await nextTick();

    expect(wrapper.get('[role="status"]').text()).toContain("正在加载");
    expect(wrapper.get('button[type="submit"]').attributes("disabled")).toBeDefined();

    pending.resolve(
      jsonResponse({
        ...initialState,
        filters: { ...initialState.filters, q: "没有结果" },
        items: [],
        pagination: { ...initialState.pagination, total_items: 0, total_pages: 1 },
      }),
    );
    await flushPromises();

    expect(wrapper.get(".empty-state").text()).toContain("没有符合");
    expect(window.location.search).toBe("?q=%E6%B2%A1%E6%9C%89%E7%BB%93%E6%9E%9C");
  });

  it("keeps existing results on failure and retries the same URL", async () => {
    const fetchImpl = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(
        jsonResponse({
          ...initialState,
          pagination: { ...initialState.pagination, total_items: 1, total_pages: 1 },
        }),
      );
    const wrapper = mount(BrowseApp, {
      props: { initialState, fetchImpl },
    });

    wrapper.get("form").element.dispatchEvent(new Event("submit", { bubbles: true }));
    await flushPromises();

    expect(wrapper.get('[role="alert"]').text()).toContain("加载失败");
    expect(wrapper.text()).toContain("已发布 Story");
    await wrapper.get("button[data-action=retry]").trigger("click");
    await flushPromises();

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
  });

  it("uses accessible pagination links and reloads on browser history navigation", async () => {
    const secondPage = {
      ...initialState,
      items: [{ ...initialState.items[0], headline: "第二页 Story" }],
      pagination: { ...initialState.pagination, page: 2 },
    };
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(secondPage))
      .mockResolvedValueOnce(jsonResponse(initialState));
    const wrapper = mount(BrowseApp, {
      props: { initialState, fetchImpl },
    });

    const nextLink = wrapper.get('a[aria-label="下一页"]');
    expect(nextLink.attributes("href")).toBe("/browse?page=2");
    await nextLink.trigger("click");
    await flushPromises();

    expect(window.location.search).toBe("?page=2");
    expect(wrapper.text()).toContain("第二页 Story");

    window.history.pushState({}, "", "/browse");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await flushPromises();

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("已发布 Story");
  });

  it("aborts an older concurrent request and ignores its late result", async () => {
    const first = deferred();
    let firstSignal;
    const fetchImpl = vi.fn((_url, options) => {
      if (fetchImpl.mock.calls.length === 1) {
        firstSignal = options.signal;
        return first.promise;
      }
      return Promise.resolve(
        jsonResponse({
          ...initialState,
          items: [{ ...initialState.items[0], headline: "最新结果" }],
        }),
      );
    });
    const wrapper = mount(BrowseApp, {
      props: { initialState, fetchImpl },
    });

    wrapper.get("form").element.dispatchEvent(new Event("submit", { bubbles: true }));
    await nextTick();
    await wrapper.get('input[name="q"]').setValue("更新后的问题");
    wrapper.get("form").element.dispatchEvent(new Event("submit", { bubbles: true }));
    await flushPromises();

    expect(firstSignal.aborted).toBe(true);
    expect(wrapper.text()).toContain("最新结果");

    first.resolve(
      jsonResponse({
        ...initialState,
        items: [{ ...initialState.items[0], headline: "过期结果" }],
      }),
    );
    await flushPromises();
    expect(wrapper.text()).not.toContain("过期结果");
  });
});
