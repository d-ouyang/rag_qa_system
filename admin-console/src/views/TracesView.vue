<script setup lang="ts">
/**
 * 问答溯源（P4-5b）—— qa_trace 分析镜像的**只读**查询页。
 *
 * --------------------------------------------------------------------------
 * 「为什么这么答 / 为什么拒答」的跨会话排查台
 * --------------------------------------------------------------------------
 * 每轮问答的完整轨迹已随 turn_meta.qa_trace 落库（P4-5 一期，面板在用户侧）；
 * 本页查的是它的分析镜像（qa_trace 表）：按终端状态 / 意图 / 日期 / 拒答差距
 * 跨会话筛选。典型问题：近 30 天多少轮零召回？拒答的都差多少分？
 * 哪个会话反复命中缓存？
 *
 * 实走路径（flow_path）由后端按 qa_flow 状态机派生后下发 —— 前端只把
 * 命中的节点点亮，**不自己再推一遍分支**（推两遍迟早漂）。
 */
import { computed, onMounted, ref } from 'vue'
import ToastStack from '@/components/ToastStack.vue'
import { formatDateTime, newToast, type ToastItem } from '@/components/ui'
import * as api from '@/api/admin'
import type { QaTraceRow } from '@/api/admin'
import { ApiError } from '@/api/http'

const PAGE_SIZE = 30

// ---------- 数据 ----------
const rows = ref<QaTraceRow[]>([])
const total = ref(0)
const decisions = ref<{ value: string; label: string }[]>([])
const intents = ref<{ value: string; label: string }[]>([])
const loading = ref(false)
const toasts = ref<ToastItem[]>([])

const filterDecision = ref('')
const filterIntent = ref('')
const filterSession = ref('')
const filterKeyword = ref('')
const filterGap = ref<number | null>(null)
const filterDates = ref<[string, string] | null>(null)
const offset = ref(0)

// ---------- 详情抽屉 ----------
const detail = ref<QaTraceRow | null>(null)
// el-drawer 要 v-model 布尔：开 = 赋 detail，关 = 置 null。桥接成一份状态，不留两份漂移。
const detailVisible = computed({
  get: () => detail.value !== null,
  set: (v: boolean) => {
    if (!v) detail.value = null
  },
})

function toast(kind: ToastItem['kind'], text: string) {
  const item = newToast(kind, text)
  toasts.value.push(item)
  setTimeout(() => dismiss(item.id), kind === 'ok' ? 2600 : 4200)
}
function dismiss(id: number) {
  toasts.value = toasts.value.filter((t) => t.id !== id)
}

async function load() {
  loading.value = true
  try {
    const res = await api.fetchQaTraces({
      decision: filterDecision.value || null,
      intent: filterIntent.value || null,
      session_id: filterSession.value.trim() || null,
      keyword: filterKeyword.value.trim() || null,
      date_from: filterDates.value?.[0] ?? null,
      date_to: filterDates.value?.[1] ?? null,
      min_top1_gap: filterGap.value,
      limit: PAGE_SIZE,
      offset: offset.value,
    })
    rows.value = res.items
    total.value = res.total
    // 下拉候选来自服务端（core/qa_flow / intent_router）：自己维护一份迟早漂
    if (res.decisions.length && !decisions.value.length) decisions.value = res.decisions
    if (res.intents.length && !intents.value.length) intents.value = res.intents
  } catch (e) {
    toast('error', e instanceof ApiError ? e.message : '加载问答轨迹失败')
  } finally {
    loading.value = false
  }
}

onMounted(load)

function applyFilter() {
  offset.value = 0
  void load()
}

function resetFilters() {
  filterDecision.value = ''
  filterIntent.value = ''
  filterSession.value = ''
  filterKeyword.value = ''
  filterGap.value = null
  filterDates.value = null
  offset.value = 0
  void load()
}

const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)
function onPage(n: number) {
  offset.value = (n - 1) * PAGE_SIZE
  void load()
}

// --------------------------------------------------------------------------- //
// 展示辅助
// --------------------------------------------------------------------------- //
/** 终端状态徽章：四色 —— 缓存命中蓝 / 闲聊灰 / 拒答橙 / 作答绿。 */
const DECISION_CLASS: Record<string, string> = {
  cache_hit: 'd-cache',
  chitchat: 'd-chit',
  zero_recall: 'd-zero',
  rag_qa: 'd-rag',
}
function decisionLabel(v: string): string {
  return decisions.value.find((d) => d.value === v)?.label ?? v
}
function intentLabel(v: string): string {
  return intents.value.find((i) => i.value === v)?.label ?? v
}
const INTENT_SOURCE_LABEL: Record<string, string> = {
  rule: '规则',
  llm: '模型',
  cache: '缓存',
}
function num(v: number | null | undefined, digits = 4): string {
  return v === null || v === undefined ? '—' : String(Math.round(v * 10 ** digits) / 10 ** digits)
}
function shortSession(id: string): string {
  return id.length > 10 ? `${id.slice(0, 10)}…` : id
}

/** 候选行（P2-23 口径的字段；缺键显示 —）。 */
interface CandRow {
  chunk_id: unknown
  file_name: unknown
  rerank_score: unknown
  vector_similarity: unknown
  passed_threshold: unknown
}
function candidatesOf(row: QaTraceRow): CandRow[] {
  // 后端候选行是宽松 dict（P2-23 口径），这里只挑展示字段；双跳 unknown 过 TS 严格检查
  return (row.candidates ?? []) as unknown as CandRow[]
}

// --------------------------------------------------------------------------- //
// 状态机图：节点坐标固定，只按 flow_path 点亮（后端派生，前端不推分支）
// --------------------------------------------------------------------------- //
interface FlowNode {
  key: string
  x: number
  y: number
  terminal?: boolean
}
/** 主链居中一列（中心 x=325），四个终端在右侧一列（中心 x=480）。 */
const FLOW_NODES: FlowNode[] = [
  { key: 'received', x: 325, y: 20 },
  { key: 'cache_check', x: 325, y: 80 },
  { key: 'cache_hit', x: 480, y: 80, terminal: true },
  { key: 'prepare', x: 325, y: 140 },
  { key: 'intent_route', x: 325, y: 200 },
  { key: 'chitchat', x: 480, y: 200, terminal: true },
  { key: 'retrieve', x: 325, y: 260 },
  { key: 'rerank', x: 325, y: 320 },
  { key: 'threshold', x: 325, y: 380 },
  { key: 'zero_recall', x: 480, y: 380, terminal: true },
  { key: 'generate', x: 325, y: 440 },
  { key: 'rag_qa', x: 480, y: 440, terminal: true },
]
/** 边 = [from, to]。竖线走主链，三个分支横线通向终端。 */
const FLOW_EDGES: [string, string][] = [
  ['received', 'cache_check'],
  ['cache_check', 'cache_hit'],
  ['cache_check', 'prepare'],
  ['prepare', 'intent_route'],
  ['intent_route', 'chitchat'],
  ['intent_route', 'retrieve'],
  ['retrieve', 'rerank'],
  ['rerank', 'threshold'],
  ['threshold', 'zero_recall'],
  ['threshold', 'generate'],
  ['generate', 'rag_qa'],
]
const NODE_W = 150
const NODE_H = 36

function nodeOf(key: string): FlowNode {
  return FLOW_NODES.find((n) => n.key === key)!
}
function pathSet(row: QaTraceRow): Set<string> {
  return new Set(row.flow_path)
}
/** 边是否实走：两端都在路径里且**相邻**（分支边只有实走时才点亮）。 */
function edgeActive(row: QaTraceRow, from: string, to: string): boolean {
  const p = row.flow_path
  const i = p.indexOf(from)
  return i >= 0 && p[i + 1] === to
}
function edgeLine([from, to]: [string, string]): { x1: number; y1: number; x2: number; y2: number } {
  const a = nodeOf(from)
  const b = nodeOf(to)
  if (a.y === b.y) {
    // 同行的分支：从主链节点右边缘 → 终端节点左边缘
    const [left, right] = a.x < b.x ? [a, b] : [b, a]
    return { x1: left.x + NODE_W / 2, y1: a.y + NODE_H / 2, x2: right.x - NODE_W / 2, y2: a.y + NODE_H / 2 }
  }
  return { x1: a.x, y1: a.y + NODE_H, x2: b.x, y2: b.y }
}
function nodeLabel(key: string): string {
  // 状态中文名由后端 STATE_LABELS 同源维护；这里按 key 兜底直译，
  // 漂移的代价只是图形上的字不对，不影响数据。
  const LABELS: Record<string, string> = {
    received: '收到问题',
    cache_check: '查问答缓存',
    cache_hit: '缓存命中',
    prepare: '并行前段',
    intent_route: '意图路由',
    chitchat: '闲聊应答',
    retrieve: '检索召回',
    rerank: '重排',
    threshold: '阈值过滤',
    zero_recall: '零召回拒答',
    generate: 'RAG 生成',
    rag_qa: '检索作答',
  }
  return LABELS[key] ?? key
}
</script>

<template>
  <div class="list-page">
    <div class="head">
      <div>
        <h1 class="page-title">问答溯源</h1>
        <p class="page-sub">
          每轮问答走了哪条路、检索到了什么、拒答差多少分 —— <strong>只读分析镜像</strong>
        </p>
      </div>
      <button class="btn" :disabled="loading" @click="load">{{ loading ? '刷新中…' : '刷新' }}</button>
    </div>

    <el-form :inline="true" class="card filters filters-form">
      <el-form-item>
        <el-select v-model="filterDecision" placeholder="全部终端状态" style="width: 150px" @change="applyFilter">
          <el-option label="全部终端状态" value="" />
          <el-option v-for="d in decisions" :key="d.value" :label="d.label" :value="d.value" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-select v-model="filterIntent" placeholder="全部意图" style="width: 140px" @change="applyFilter">
          <el-option label="全部意图" value="" />
          <el-option v-for="i in intents" :key="i.value" :label="i.label" :value="i.value" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-input v-model="filterSession" placeholder="按会话 id 精确筛" clearable style="width: 200px" @input="applyFilter" />
      </el-form-item>
      <el-form-item>
        <el-input v-model="filterKeyword" placeholder="在问题里搜" clearable @input="applyFilter" />
      </el-form-item>
      <el-form-item>
        <el-date-picker
          v-model="filterDates"
          type="daterange"
          value-format="YYYY-MM-DD"
          start-placeholder="开始日期"
          end-placeholder="结束日期"
          style="width: 240px"
          @change="applyFilter"
        />
      </el-form-item>
      <el-form-item>
        <el-input-number
          v-model="filterGap"
          :min="0"
          :max="1"
          :step="0.05"
          :precision="2"
          placeholder="差距 ≥"
          controls-position="right"
          style="width: 120px"
          @change="applyFilter"
        />
        <span class="muted gap-hint">拒答差距 ≥</span>
      </el-form-item>
      <el-form-item>
        <button class="btn btn-ghost" @click="resetFilters">清空筛选</button>
      </el-form-item>
    </el-form>

    <div class="card table-wrap">
      <table class="grid">
        <thead>
          <tr>
            <th>时间</th>
            <th>终端状态</th>
            <th>意图</th>
            <th>问题</th>
            <th>top1 / 阈值</th>
            <th>耗时</th>
            <th>会话</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td class="muted nowrap">{{ formatDateTime(row.created_at) }}</td>
            <td class="nowrap">
              <span class="badge" :class="DECISION_CLASS[row.decision]">{{ decisionLabel(row.decision) }}</span>
            </td>
            <td class="nowrap">
              {{ intentLabel(row.intent) }}
              <span class="muted">· {{ INTENT_SOURCE_LABEL[row.intent_source] ?? row.intent_source }}</span>
            </td>
            <td class="q">
              {{ row.question }}
              <span v-if="row.hyde_used" class="chip">HyDE</span>
              <span v-if="row.bm25_hits != null" class="chip">BM25 {{ row.bm25_hits }}</span>
            </td>
            <td class="mono nowrap">
              <template v-if="row.top1_before_filter != null">
                {{ num(row.top1_before_filter) }} / {{ num(row.threshold) }}
                <span v-if="row.decision === 'zero_recall'" class="gap">(差 {{ num(row.top1_gap) }})</span>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td class="mono nowrap">{{ Math.round(row.elapsed_ms) }}ms</td>
            <td class="mono muted nowrap" :title="row.session_id">{{ shortSession(row.session_id) }}</td>
            <td class="nowrap"><button class="btn btn-ghost" @click="detail = row">详情</button></td>
          </tr>
          <tr v-if="!rows.length">
            <td colspan="8" class="empty">没有符合条件的轨迹</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pager">
      <el-pagination
        layout="total, prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="currentPage"
        @current-change="onPage"
      />
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="轨迹详情" size="640px">
      <template v-if="detail">
        <div class="detail-head">
          <span class="badge" :class="DECISION_CLASS[detail.decision]">{{ decisionLabel(detail.decision) }}</span>
          <span class="muted">{{ formatDateTime(detail.created_at) }} · {{ Math.round(detail.elapsed_ms) }}ms</span>
        </div>

        <!-- 实走路径高亮（flow_path 由后端派生） -->
        <h3 class="sec">实走路径</h3>
        <div class="flow-wrap">
          <svg viewBox="0 0 580 496" class="flow-svg" role="img" aria-label="问答流程状态机">
            <g v-for="[f, t] in FLOW_EDGES" :key="`${f}-${t}`">
              <line
                v-bind="edgeLine([f, t])"
                :class="edgeActive(detail, f, t) ? 'edge on' : 'edge'"
              />
            </g>
            <g v-for="n in FLOW_NODES" :key="n.key">
              <rect
                :x="n.x - NODE_W / 2" :y="n.y" :width="NODE_W" :height="NODE_H" rx="8"
                :class="[
                  'node',
                  pathSet(detail).has(n.key) ? (n.terminal ? 'on-term' : 'on') : '',
                ]"
              />
              <text :x="n.x" :y="n.y + NODE_H / 2 + 4" text-anchor="middle"
                    :class="pathSet(detail).has(n.key) ? 't-on' : 't-off'">
                {{ nodeLabel(n.key) }}
              </text>
            </g>
          </svg>
        </div>

        <!-- 关键指标 -->
        <h3 class="sec">关键指标</h3>
        <div class="metrics">
          <div class="m"><span class="k">意图</span><span>{{ intentLabel(detail.intent) }}（{{ INTENT_SOURCE_LABEL[detail.intent_source] ?? detail.intent_source ?? '—' }}）</span></div>
          <div class="m"><span class="k">文档 / 来源</span><span>{{ detail.docs_returned }} / {{ detail.sources_count }}</span></div>
          <div class="m"><span class="k">阈值</span><span>{{ num(detail.threshold) }}</span></div>
          <div class="m"><span class="k">过滤前 top1</span><span>{{ num(detail.top1_before_filter) }}</span></div>
          <div class="m"><span class="k">被过滤</span><span>{{ detail.filtered_count ?? '—' }}</span></div>
          <div class="m"><span class="k">差多少分</span><span>{{ num(detail.top1_gap) }}</span></div>
          <div class="m"><span class="k">BM25 命中</span><span>{{ detail.bm25_hits ?? '未开启' }}</span></div>
          <div class="m"><span class="k">仅 BM25</span><span>{{ detail.bm25_only ?? '—' }}</span></div>
          <div class="m"><span class="k">RRF top</span><span class="mono">{{ detail.rrf_top ?? '未开启' }}</span></div>
          <div class="m"><span class="k">HyDE</span><span>{{ detail.hyde_used ? '用了' : '没用' }}</span></div>
        </div>

        <!-- 问句链 -->
        <h3 class="sec">问句链</h3>
        <div class="qs">
          <div class="m"><span class="k">原始问题</span><span>{{ detail.question }}</span></div>
          <div class="m"><span class="k">独立问句</span><span>{{ detail.standalone_question ?? '—' }}</span></div>
          <div class="m"><span class="k">改写产物</span><span>{{ detail.rewritten_query ?? '—' }}</span></div>
          <div class="m"><span class="k">HyDE 向量文本</span><span>{{ detail.vector_query ?? '—' }}</span></div>
        </div>

        <!-- 候选清单 -->
        <template v-if="candidatesOf(detail).length">
          <h3 class="sec">候选清单（重排前）</h3>
          <table class="grid cands">
            <thead>
              <tr><th>#</th><th>来源</th><th>切片</th><th>重排分</th><th>向量分</th><th>过阈值</th></tr>
            </thead>
            <tbody>
              <tr v-for="(c, i) in candidatesOf(detail)" :key="i">
                <td class="muted">{{ i + 1 }}</td>
                <td>{{ c.file_name ?? '—' }}</td>
                <td class="mono">{{ c.chunk_id ?? '—' }}</td>
                <td class="mono">{{ num(c.rerank_score as number | null) }}</td>
                <td class="mono">{{ num(c.vector_similarity as number | null) }}</td>
                <td>{{ c.passed_threshold ? '✓' : '✗' }}</td>
              </tr>
            </tbody>
          </table>
        </template>
        <p v-else class="muted">这一轮没有跑检索（缓存命中或闲聊），无候选可看。</p>
      </template>
    </el-drawer>

    <ToastStack :items="toasts" @dismiss="dismiss" />
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
}
.filters {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.gap-hint {
  margin-left: 6px;
  font-size: 12px;
}
.table-wrap {
  overflow: auto;
}
.nowrap {
  white-space: nowrap;
}
.q {
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
}
.d-cache { background: #2563eb; }
.d-chit { background: #64748b; }
.d-zero { background: #d97706; }
.d-rag { background: #059669; }
.chip {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 4px;
  font-size: 11px;
  background: var(--bg-3, #eef2f7);
  color: var(--text-2, #475569);
}
.gap { color: #d97706; }
.pager {
  display: flex;
  align-items: center;
  margin-top: 12px;
}

/* ---- 详情抽屉 ---- */
.detail-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.sec {
  margin: 18px 0 8px;
  font-size: 14px;
  color: var(--text-1, #111827);
}
.flow-wrap {
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 10px;
  padding: 8px;
  background: #fff;
}
.flow-svg {
  width: 100%;
  height: auto;
  display: block;
}
.flow-svg .node {
  fill: #f1f5f9;
  stroke: #e2e8f0;
}
.flow-svg .node.on { fill: #0f766e; stroke: #0f766e; }
.flow-svg .node.on-term { fill: #065f46; stroke: #065f46; }
.flow-svg .t-off { fill: #94a3b8; font-size: 13px; }
.flow-svg .t-on { fill: #fff; font-size: 13px; font-weight: 600; }
.flow-svg .edge { stroke: #e2e8f0; stroke-width: 1.5; }
.flow-svg .edge.on { stroke: #0f766e; stroke-width: 2.5; }

.metrics, .qs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px 18px;
  font-size: 13px;
}
.metrics .m, .qs .m {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.m .k {
  flex: 0 0 84px;
  color: var(--text-3, #94a3b8);
}
.cands {
  margin-top: 4px;
}
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.empty {
  text-align: center;
  color: var(--text-3, #94a3b8);
  padding: 24px 0;
}
</style>
