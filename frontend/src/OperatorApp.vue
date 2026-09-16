<script setup>
import { computed, onMounted, ref } from "vue";

/** @typedef {(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>} OperatorFetch */

/** @type {{fetchImpl: OperatorFetch}} */
const props = defineProps({
  fetchImpl: {
    type: Function,
    default: (input, init) => globalThis.fetch(input, init),
  },
});

const dashboard = ref(null);
const csrfToken = ref("");
const selectedPlan = ref(null);
const rawDocument = ref(null);
const loading = ref(true);
const detailLoading = ref(false);
const error = ref("");
const loggedOut = ref(false);

const statusText = computed(() => {
  if (loggedOut.value) return "已注销";
  if (loading.value) return "正在加载 Operator 状态…";
  if (detailLoading.value) return "正在加载检查详情…";
  return "Operator 状态已更新";
});

async function jsonRequest(url, init) {
  const response = await props.fetchImpl(url, {
    headers: { Accept: "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) throw new Error(`Operator request failed with status ${response.status}`);
  return response.json();
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const [session, dashboardPayload] = await Promise.all([
      jsonRequest("/api/operator/session"),
      jsonRequest("/api/operator/dashboard"),
    ]);
    csrfToken.value = session.csrf_token;
    dashboard.value = dashboardPayload;
  } catch (_caught) {
    error.value = "Operator 状态加载失败。";
  } finally {
    loading.value = false;
  }
}

async function inspectPlan(planId) {
  detailLoading.value = true;
  rawDocument.value = null;
  error.value = "";
  try {
    selectedPlan.value = await jsonRequest(`/api/operator/plans/${planId}`);
  } catch (_caught) {
    error.value = "Digest Plan 检查数据加载失败。";
  } finally {
    detailLoading.value = false;
  }
}

async function inspectDocument(documentId) {
  detailLoading.value = true;
  error.value = "";
  try {
    rawDocument.value = await jsonRequest(`/api/operator/document-versions/${documentId}`);
  } catch (_caught) {
    error.value = "Document Version 原文加载失败。";
  } finally {
    detailLoading.value = false;
  }
}

async function logout() {
  error.value = "";
  try {
    const response = await props.fetchImpl("/operator/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken.value },
    });
    if (!response.ok) throw new Error(`Logout failed with status ${response.status}`);
    loggedOut.value = true;
    dashboard.value = null;
    selectedPlan.value = null;
    rawDocument.value = null;
  } catch (_caught) {
    error.value = "注销失败；当前 Session 未被假定失效。";
  }
}

function completionLabel(plan) {
  return plan.completion ? plan.completion.kind : "未完成";
}

function followUpLabel(plan) {
  return plan.index_follow_up ? plan.index_follow_up.state : "无跟进";
}

onMounted(load);
</script>

<template>
  <header class="operator-header">
    <div>
      <p class="eyebrow">Private read surface</p>
      <h1>Operator Console</h1>
      <p v-if="dashboard" class="operator-identity">
        GitHub {{ dashboard.operator.github_login }} · ID {{ dashboard.operator.github_user_id }}
      </p>
    </div>
    <button v-if="dashboard" type="button" data-action="logout" @click="logout">注销</button>
  </header>

  <p class="operator-status" role="status" aria-live="polite">{{ statusText }}</p>
  <div v-if="error" class="operator-error" role="alert">
    <p>{{ error }}</p><button type="button" @click="load">重新加载</button>
  </div>
  <p v-if="loggedOut" class="operator-empty">已注销。重新进入此页面可再次通过 GitHub 登录。</p>

  <template v-if="dashboard">
    <section aria-labelledby="dashboard-heading">
      <h2 id="dashboard-heading">今日 Dashboard</h2>
      <div class="metric-grid">
        <article><strong>待处理 Story {{ dashboard.pending_story_count }}</strong><span>当前审核队列</span></article>
        <article><strong>{{ dashboard.scheduler?.state || "无状态" }}</strong><span>Scheduler</span></article>
        <article><strong>{{ dashboard.plans.length }}</strong><span>历史 Plan</span></article>
      </div>
    </section>

    <section aria-labelledby="sources-heading">
      <h2 id="sources-heading">来源健康</h2>
      <div class="operator-table-wrap">
        <table>
          <thead><tr><th>来源</th><th>健康</th><th>最近结果</th><th>待处理</th></tr></thead>
          <tbody>
            <tr v-for="source in dashboard.sources" :key="source.id">
              <th scope="row">{{ source.name }}<small>{{ source.publisher }}</small></th>
              <td>{{ source.health }}</td><td>{{ source.recent_result }}</td><td>{{ source.pending_drafts }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section aria-labelledby="plans-heading">
      <h2 id="plans-heading">Digest Plan 历史</h2>
      <div v-if="dashboard.plans.length" class="plan-list">
        <button
          v-for="plan in dashboard.plans"
          :key="plan.id"
          type="button"
          class="plan-row"
          :data-plan-id="plan.id"
          @click="inspectPlan(plan.id)"
        >
          <strong>{{ plan.publication_date }} · v{{ plan.version }}</strong>
          <span>{{ plan.included_story_count }} included · {{ completionLabel(plan) }} · index {{ followUpLabel(plan) }}</span>
          <span>{{ plan.blocker_count }} blockers · {{ plan.warning_count }} warnings</span>
        </button>
      </div>
      <p v-else class="operator-empty">尚无 Digest Plan。</p>
    </section>

    <section v-if="selectedPlan" aria-labelledby="plan-detail-heading">
      <h2 id="plan-detail-heading">Plan 检查 · {{ selectedPlan.publication_date }} v{{ selectedPlan.version }}</h2>
      <p>{{ selectedPlan.digest_summary }}</p>
      <div v-if="selectedPlan.blockers.length" class="notice blocker">
        <h3>Blockers</h3><ul><li v-for="item in selectedPlan.blockers" :key="item.code">{{ item.message }}</li></ul>
      </div>
      <div v-if="selectedPlan.warnings.length" class="notice warning">
        <h3>Warnings</h3><ul><li v-for="item in selectedPlan.warnings" :key="item.code">{{ item.message }}</li></ul>
      </div>

      <article v-for="story in selectedPlan.stories" :key="story.id" class="operator-story">
        <p class="story-state">{{ story.inclusion }} · {{ story.primary_topic }} · {{ story.publisher }}</p>
        <h3>{{ story.headline }}</h3>
        <p>{{ story.summary }}</p><p>{{ story.why_it_matters }}</p>
        <p><a v-if="story.source_url" :href="story.source_url" rel="noreferrer">来源链接</a></p>
        <button type="button" :data-document-id="story.primary_document_version_id" @click="inspectDocument(story.primary_document_version_id)">查看原始 Document Version</button>
        <details v-for="claim in story.claims" :key="claim.id" open>
          <summary>{{ claim.text }}</summary>
          <blockquote v-for="evidence in claim.evidence" :key="evidence.id">
            <p>{{ evidence.exact_text }}</p>
            <footer>{{ evidence.role }} · {{ evidence.relation }} · {{ evidence.publisher }}</footer>
            <a v-if="evidence.source_url" :href="evidence.source_url" rel="noreferrer">证据来源</a>
            <button type="button" :data-document-id="evidence.document_version_id" @click="inspectDocument(evidence.document_version_id)">检查证据原文</button>
          </blockquote>
        </details>
      </article>
    </section>

    <section v-if="rawDocument" class="raw-document" data-raw-document aria-labelledby="raw-heading">
      <h2 id="raw-heading">原始 Document Version</h2>
      <p>{{ rawDocument.title }}</p>
      <a v-if="rawDocument.source_url" :href="rawDocument.source_url" rel="noreferrer">原始来源</a>
      <pre>{{ rawDocument.body }}</pre>
    </section>
  </template>
</template>
