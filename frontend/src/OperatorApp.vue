<script setup>
import { computed, onMounted, ref } from "vue";

/** @typedef {(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>} OperatorFetch */

/** @type {{fetchImpl: OperatorFetch, idempotencyKeyFactory: () => string, nowFactory: () => Date}} */
const props = defineProps({
  fetchImpl: {
    type: Function,
    default: (input, init) => globalThis.fetch(input, init),
  },
  idempotencyKeyFactory: {
    type: Function,
    default: () => globalThis.crypto.randomUUID(),
  },
  nowFactory: {
    type: Function,
    default: () => new Date(),
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
const mutationLoading = ref(false);
const removalReasons = ref({});
const approvalResult = ref(null);
const preparationDate = ref(shanghaiDate(props.nowFactory()));

function shanghaiDate(now) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${values.year}-${values.month}-${values.day}`;
}

const statusText = computed(() => {
  if (loggedOut.value) return "已注销";
  if (loading.value) return "正在加载 Operator 状态…";
  if (detailLoading.value) return "正在加载检查详情…";
  if (mutationLoading.value) return "正在提交精确 Editorial 操作…";
  return "Operator 状态已更新";
});

async function jsonRequest(url, init) {
  const response = await props.fetchImpl(url, {
    headers: { Accept: "application/json", ...(init?.headers || {}) },
    ...init,
  });
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    const error = new Error(body?.detail || `Operator request failed with status ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return body;
}

async function mutate(url, body) {
  return jsonRequest(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken.value,
      "Idempotency-Key": props.idempotencyKeyFactory(),
    },
    body: JSON.stringify(body),
  });
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
    approvalResult.value = selectedPlan.value.completion;
  } catch (_caught) {
    error.value = "Digest Plan 检查数据加载失败。";
  } finally {
    detailLoading.value = false;
  }
}

function exactPlanInput(plan) {
  return {
    expected_version: plan.version,
    expected_content_hash: plan.content_hash,
  };
}

async function preparePlan() {
  if (mutationLoading.value) return;
  mutationLoading.value = true;
  error.value = "";
  approvalResult.value = null;
  const latest = dashboard.value.plans.find(
    (plan) => plan.publication_date === preparationDate.value && plan.is_latest !== false,
  );
  try {
    selectedPlan.value = await mutate("/api/operator/plans/prepare", {
      publication_date: preparationDate.value,
      expected_plan: latest
        ? { id: latest.id, version: latest.version, content_hash: latest.content_hash }
        : null,
    });
    dashboard.value = await jsonRequest("/api/operator/dashboard");
  } catch (caught) {
    error.value = mutationError(caught, "Digest Plan 准备失败。");
  } finally {
    mutationLoading.value = false;
  }
}

async function removeStory(story) {
  if (mutationLoading.value) return;
  const reason = (removalReasons.value[story.stable_key] || "").trim();
  if (!reason) {
    error.value = "移除 Story 必须填写原因。";
    return;
  }
  mutationLoading.value = true;
  error.value = "";
  try {
    selectedPlan.value = await mutate(
      `/api/operator/plans/${selectedPlan.value.id}/stories/${encodeURIComponent(story.stable_key)}/remove`,
      { ...exactPlanInput(selectedPlan.value), reason },
    );
    removalReasons.value = {};
    dashboard.value = await jsonRequest("/api/operator/dashboard");
  } catch (caught) {
    error.value = mutationError(caught, "Story 移除失败。");
  } finally {
    mutationLoading.value = false;
  }
}

async function approvePlan() {
  if (mutationLoading.value) return;
  mutationLoading.value = true;
  error.value = "";
  try {
    approvalResult.value = await mutate(
      `/api/operator/plans/${selectedPlan.value.id}/approve`,
      exactPlanInput(selectedPlan.value),
    );
    selectedPlan.value.completion = approvalResult.value;
    selectedPlan.value.index_follow_up = approvalResult.value.follow_up;
    dashboard.value = await jsonRequest("/api/operator/dashboard");
  } catch (caught) {
    error.value = mutationError(caught, "Digest Plan 批准失败。");
  } finally {
    mutationLoading.value = false;
  }
}

async function retryFollowUp() {
  if (mutationLoading.value) return;
  mutationLoading.value = true;
  error.value = "";
  try {
    const followUp = await mutate(
      `/api/operator/plans/${selectedPlan.value.id}/follow-up/retry`,
      exactPlanInput(selectedPlan.value),
    );
    selectedPlan.value.index_follow_up = followUp;
    if (approvalResult.value) approvalResult.value.follow_up = followUp;
    dashboard.value = await jsonRequest("/api/operator/dashboard");
  } catch (caught) {
    error.value = mutationError(caught, "索引跟进重试失败。");
  } finally {
    mutationLoading.value = false;
  }
}

function mutationError(caught, fallback) {
  if (caught?.status === 409) return `${caught.message} 请重新加载最新 Plan 后继续。`;
  return caught?.message || fallback;
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

function planCanMutate(plan) {
  return plan.is_latest !== false && !plan.completion;
}

onMounted(load);
</script>

<template>
  <header class="operator-header">
    <div>
      <p class="eyebrow">Protected editorial workflow</p>
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
      <form class="operator-action" @submit.prevent="preparePlan">
        <label for="plan-publication-date">日报日期</label>
        <input id="plan-publication-date" v-model="preparationDate" type="date" required>
        <button type="submit" data-action="prepare-plan" :disabled="mutationLoading">
          准备或重新准备 Plan
        </button>
      </form>
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
        <div
          v-if="story.inclusion === 'included' && planCanMutate(selectedPlan)"
          class="operator-action"
        >
          <label :for="`removal-reason-${story.id}`">从当前 Plan 移除此 Story 的原因</label>
          <textarea
            :id="`removal-reason-${story.id}`"
            v-model="removalReasons[story.stable_key]"
            :data-removal-reason="story.stable_key"
            maxlength="1000"
            required
          />
          <button
            type="button"
            :data-action="`remove-${story.stable_key}`"
            :disabled="mutationLoading"
            @click="removeStory(story)"
          >从 Plan 移除</button>
        </div>
      </article>

      <div v-if="planCanMutate(selectedPlan)" class="operator-action">
        <p>批准只绑定当前显示的 v{{ selectedPlan.version }} / {{ selectedPlan.content_hash }}。</p>
        <button
          type="button"
          data-action="approve-plan"
          :disabled="mutationLoading || selectedPlan.blockers.length > 0"
          @click="approvePlan"
        >批准并完成</button>
      </div>

      <div v-if="approvalResult" class="notice" data-approval-result>
        <p v-if="approvalResult.kind === 'no-publication'">本日已确认不发布；未创建空 Digest。</p>
        <p v-else>已发布精确 Plan。</p>
        <a v-if="approvalResult.public_url" :href="approvalResult.public_url">查看公开日报</a>
        <p v-if="approvalResult.follow_up">
          索引跟进：{{ approvalResult.follow_up.state }} · 尝试 {{ approvalResult.follow_up.attempt_count }}
        </p>
      </div>

      <div v-if="selectedPlan.index_follow_up" class="operator-action" data-follow-up-status>
        <p>
          Retrieval Index Follow-Up：{{ selectedPlan.index_follow_up.state }}
          · 尝试 {{ selectedPlan.index_follow_up.attempt_count }}
        </p>
        <p v-if="selectedPlan.index_follow_up.last_error">{{ selectedPlan.index_follow_up.last_error }}</p>
        <button
          v-if="selectedPlan.index_follow_up.state === 'failed'"
          type="button"
          data-action="retry-follow-up"
          :disabled="mutationLoading"
          @click="retryFollowUp"
        >安全重试索引跟进</button>
      </div>
    </section>

    <section v-if="rawDocument" class="raw-document" data-raw-document aria-labelledby="raw-heading">
      <h2 id="raw-heading">原始 Document Version</h2>
      <p>{{ rawDocument.title }}</p>
      <a v-if="rawDocument.source_url" :href="rawDocument.source_url" rel="noreferrer">原始来源</a>
      <pre>{{ rawDocument.body }}</pre>
    </section>
  </template>
</template>
