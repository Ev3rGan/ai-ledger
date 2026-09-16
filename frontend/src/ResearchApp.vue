<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";

/** @typedef {import("./research-stream-client.js").ResearchSnapshot} ResearchSnapshot */
/**
 * @typedef {object} ResearchClient
 * @property {(question: string) => Promise<ResearchSnapshot>} start
 * @property {() => void} cancel
 * @property {(listener: (snapshot: ResearchSnapshot) => void) => (() => void)} subscribe
 */

/** @type {{client: ResearchClient, examples: string[]}} */
const props = defineProps({
  client: { type: Object, required: true },
  examples: { type: Array, default: () => [] },
});

const question = ref("");
const questionInput = ref(null);
const current = ref({
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
});
let unsubscribe = null;

const busy = computed(() => ["connecting", "streaming"].includes(current.value.phase));
const statusText = computed(() => {
  if (current.value.phase === "connecting") return "正在连接…";
  if (current.value.phase === "answered") return "回答完成";
  if (current.value.phase === "refused") return "已拒答";
  if (current.value.phase === "failed") return "请求失败";
  if (current.value.phase === "cancelled") return "已取消";
  const progress = {
    retrieving: "正在解释问题并检索…",
    "retrieval-degraded": "检索降级：正在使用已声明的 fallback…",
    "evidence-assembled": "Evidence Set 已汇总…",
    generating: "正在生成有依据的答案…",
    "verifying-citations": "正在校验引用…",
  };
  const progressText = progress[current.value.progress] || "";
  if (
    current.value.retrievalDegraded &&
    current.value.progress !== "retrieval-degraded"
  ) {
    const fallback = current.value.retrievalFallback
      ? `（${current.value.retrievalFallback}）`
      : "";
    return `检索降级${fallback}；${progressText}`;
  }
  return progressText;
});
const showAlert = computed(() =>
  ["refused", "failed"].includes(current.value.phase) && current.value.message,
);

async function ask() {
  const submitted = question.value.trim();
  if (!submitted) return;
  try {
    await props.client.start(submitted);
  } catch {
    // The client owns and publishes transport/protocol failure state.
  }
}

function cancel() {
  props.client.cancel();
}

function retry() {
  question.value = current.value.question || question.value;
  return ask();
}

async function chooseExample(example) {
  question.value = example;
  await nextTick();
  questionInput.value?.focus();
}

function citationLabel(citation) {
  const timeSemantics = {
    event: "事件时间",
    "source-publication": "来源发布时间",
    discovery: "发现时间",
    editorial: "编辑时间",
    "digest-publication": "Digest 发布时间",
  };
  const statements = (citation.statement_support || [])
    .map((support) => {
      const timeLabel = timeSemantics[support.time_semantic];
      return `陈述 ${support.statement_index}${timeLabel ? `（${timeLabel}）` : ""}`;
    })
    .join("、");
  const roles = {
    primary: "第一方证据",
    independent: "独立证据",
    secondary: "二手证据",
  };
  const role = roles[citation.evidence_role] || "公开证据";
  return `${statements} — ${citation.story_title} — ${citation.claim_text} — ${role} — ${citation.evidence_text}`;
}

onMounted(() => {
  unsubscribe = props.client.subscribe((snapshot) => {
    current.value = snapshot;
  });
});
onUnmounted(() => unsubscribe?.());
</script>

<template>
  <section aria-labelledby="research-examples-heading">
    <h2 id="research-examples-heading">试试这些问题</h2>
    <div class="research-examples">
      <button
        v-for="example in examples"
        :key="example"
        class="research-example"
        type="button"
        @click="chooseExample(example)"
        @keydown.enter.prevent="chooseExample(example)"
        @keydown.space.prevent="chooseExample(example)"
      >{{ example }}</button>
      <p v-if="examples.length === 0" class="muted">当前没有可由已发布知识支持的示例问题。</p>
    </div>
  </section>

  <section class="research-panel" aria-labelledby="research-question-heading">
    <h2 id="research-question-heading">如何提问</h2>
    <p>选择一个示例或输入一个具体问题；示例只会填入文本框，提交仍由你决定。</p>
    <form id="research-form" @submit.prevent="ask">
      <label for="research-question">你的问题</label>
      <textarea
        id="research-question"
        ref="questionInput"
        v-model="question"
        name="question"
        maxlength="500"
        rows="4"
        required
      ></textarea>
      <div class="research-actions">
        <button type="submit" :disabled="busy">查找答案</button>
        <button v-if="busy" type="button" data-action="cancel" @click="cancel">取消</button>
      </div>
    </form>

    <div class="research-results" aria-live="polite">
      <p role="status">{{ statusText }}</p>
      <div v-if="showAlert" class="interaction-error" role="alert">
        <p>{{ current.message }}</p>
        <button type="button" data-action="retry" @click="retry">重试</button>
      </div>
      <p v-if="current.answer" class="research-answer">{{ current.answer }}</p>
      <ul v-if="current.citations.length" class="research-citations">
        <li v-for="citation in current.citations" :key="citation.evidence_url">
          <a :href="citation.evidence_url">{{ citationLabel(citation) }}</a>
        </li>
      </ul>
    </div>
  </section>
</template>
