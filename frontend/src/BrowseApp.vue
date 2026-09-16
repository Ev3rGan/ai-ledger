<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";

/** @typedef {{q: string | null, source: string | null, topic: string | null, date: string | null}} BrowseFilters */
/** @typedef {{url: string, headline: string, summary: string | null, publisher: string, topic: string | null, secondary_topics: string[], published_at: string | null}} BrowseStory */
/** @typedef {{page: number, page_size: number, total_items: number, total_pages: number}} BrowsePagination */
/** @typedef {{sources: string[], topics: string[]}} BrowseFacets */
/** @typedef {{filters: BrowseFilters, facets: BrowseFacets, items: BrowseStory[], pagination: BrowsePagination}} BrowsePayload */
/** @typedef {(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>} BrowseFetch */

/** @type {{initialState: BrowsePayload, endpoint: string, fetchImpl: BrowseFetch}} */
const props = defineProps({
  initialState: { type: Object, required: true },
  endpoint: { type: String, default: "/api/public/browse" },
  fetchImpl: {
    type: Function,
    default: (input, init) => globalThis.fetch(input, init),
  },
});

/** @type {import("vue").Ref<BrowsePayload>} */
const state = ref(props.initialState);
const filters = reactive({ ...props.initialState.filters });
const loading = ref(false);
const error = ref("");
let controller = null;
let requestId = 0;
let lastRequest = { filters: { ...filters }, page: props.initialState.pagination.page };

const statusText = computed(() =>
  loading.value
    ? "正在加载 Browse 结果…"
    : `找到 ${state.value.pagination.total_items} 条 Story`,
);

function queryFor(nextFilters, page) {
  const query = new URLSearchParams();
  for (const name of ["q", "source", "topic", "date"]) {
    const value = nextFilters[name];
    if (value) query.set(name, value);
  }
  if (page > 1) query.set("page", String(page));
  return query;
}

function pageHref(page) {
  const query = queryFor(filters, page).toString();
  return query ? `/browse?${query}` : "/browse";
}

/**
 * @param {BrowseFilters} nextFilters
 * @param {number} page
 * @param {{push?: boolean}} options
 */
async function load(nextFilters, page, { push = true } = {}) {
  const normalizedFilters = {
    q: nextFilters.q || null,
    source: nextFilters.source || null,
    topic: nextFilters.topic || null,
    date: nextFilters.date || null,
  };
  lastRequest = { filters: { ...normalizedFilters }, page };
  const query = queryFor(normalizedFilters, page);
  if (push) {
    const search = query.toString();
    window.history.pushState({}, "", search ? `/browse?${search}` : "/browse");
  }

  const currentRequest = ++requestId;
  controller?.abort();
  controller = new AbortController();
  loading.value = true;
  error.value = "";
  try {
    const search = query.toString();
    const response = await props.fetchImpl(
      search ? `${props.endpoint}?${search}` : props.endpoint,
      { headers: { Accept: "application/json" }, signal: controller.signal },
    );
    if (currentRequest !== requestId) return;
    if (!response.ok) throw new Error(`Browse request failed with status ${response.status}`);
    const payload = await response.json();
    if (currentRequest !== requestId) return;
    state.value = payload;
    Object.assign(filters, payload.filters);
  } catch (caught) {
    if (currentRequest !== requestId || caught?.name === "AbortError") return;
    error.value = "Browse 结果加载失败；现有结果仍保留。";
  } finally {
    if (currentRequest === requestId) {
      loading.value = false;
      controller = null;
    }
  }
}

function submit() {
  return load(filters, 1);
}

function goToPage(page) {
  return load(filters, page);
}

function retry() {
  return load(lastRequest.filters, lastRequest.page, { push: false });
}

function restoreFromLocation() {
  const query = new URLSearchParams(window.location.search);
  const restored = {
    q: query.get("q"),
    source: query.get("source"),
    topic: query.get("topic"),
    date: query.get("date"),
  };
  Object.assign(filters, restored);
  const requestedPage = Number.parseInt(query.get("page") || "1", 10);
  const page = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1;
  void load(restored, page, { push: false });
}

function displayDate(value) {
  if (!value) return "时间未知";
  return value.slice(0, 10);
}

function storyTopics(story) {
  return [story.topic, ...story.secondary_topics].filter(Boolean).join(" · ");
}

onMounted(() => window.addEventListener("popstate", restoreFromLocation));
onUnmounted(() => {
  window.removeEventListener("popstate", restoreFromLocation);
  controller?.abort();
});
</script>

<template>
  <form class="browse-form" method="get" action="/browse" @submit.prevent="submit">
    <label>关键词<input v-model="filters.q" name="q" type="search" placeholder="模型、公司或事实" /></label>
    <label>发布者<select v-model="filters.source" name="source"><option :value="null">全部来源</option><option v-for="source in state.facets.sources" :key="source" :value="source">{{ source }}</option></select></label>
    <label>主题<select v-model="filters.topic" name="topic"><option :value="null">全部主题</option><option v-for="topic in state.facets.topics" :key="topic" :value="topic">{{ topic }}</option></select></label>
    <label>原始发布日期<input v-model="filters.date" name="date" type="date" /></label>
    <button type="submit" :disabled="loading">筛选</button>
  </form>

  <p role="status" class="muted" aria-live="polite">{{ statusText }}</p>
  <div v-if="error" class="interaction-error" role="alert">
    <p>{{ error }}</p>
    <button type="button" data-action="retry" @click="retry">重试</button>
  </div>

  <div v-if="state.items.length" class="story-grid" :aria-busy="loading">
    <article v-for="story in state.items" :key="story.url" class="story-card">
      <p v-if="storyTopics(story)" class="topic">{{ storyTopics(story) }}</p>
      <h2><a :href="story.url">{{ story.headline }}</a></h2>
      <p v-if="story.summary">{{ story.summary }}</p>
      <p class="story-meta"><span>{{ story.publisher }}</span><span>{{ displayDate(story.published_at) }}</span></p>
    </article>
  </div>
  <p v-else class="empty-state">没有符合这些条件的已发布 Story。</p>

  <nav v-if="state.pagination.total_pages > 1" class="pagination" aria-label="Browse 分页">
    <a
      v-if="state.pagination.page > 1"
      aria-label="上一页"
      :href="pageHref(state.pagination.page - 1)"
      @click.prevent="goToPage(state.pagination.page - 1)"
    >上一页</a>
    <span>第 {{ state.pagination.page }} / {{ state.pagination.total_pages }} 页</span>
    <a
      v-if="state.pagination.page < state.pagination.total_pages"
      aria-label="下一页"
      :href="pageHref(state.pagination.page + 1)"
      @click.prevent="goToPage(state.pagination.page + 1)"
    >下一页</a>
  </nav>
</template>
