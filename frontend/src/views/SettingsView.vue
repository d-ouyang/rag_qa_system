<script setup lang="ts">
/**
 * 系统设置页（右侧内容区 · settings 视图）。
 *
 * 两层内容（P4-2）：
 *   ① 「运行时开关」卡片（页面最上方）：来自 /admin/config 注册表，
 *      superadmin 可改、**确认修改后下一个请求即生效**（无需重启）；
 *      非 superadmin 请求 403 → 整块不渲染，页面退回纯只读。
 *   ② 只读配置卡片：来自 /system/settings 汇总，配置来源于配置文件，
 *      修改需重启（卡片下方有 note 说明的属于「改了还要重建索引」的重灾区）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useUiStore } from '@/stores/ui'
import { ApiError } from '@/api/http'
import { getRuntimeConfig, patchRuntimeConfig, type RuntimeFlag } from '@/api/system'

const settingsStore = useSettingsStore()
const ui = useUiStore()

onMounted(() => {
  void settingsStore.refresh()
  void loadFlags()
})

const s = computed(() => settingsStore.settings)

/** 通用键值渲染配置：[key 展示名, 取值函数, 值格式化] */
const groups = computed(() => {
  if (!s.value) return []
  return [
    {
      title: '项目信息',
      icon: '📦',
      rows: [
        ['项目名称', s.value.project.name],
        ['版本', `v${s.value.project.version}`],
        ['监听地址', `${s.value.project.api_host}:${s.value.project.api_port}`],
        ['日志级别', s.value.project.log_level],
      ] as [string, string][],
      note: '本卡为运行信息与基础设施配置，修改需重启服务。',
    },
    {
      title: '大语言模型（LLM）',
      icon: '🤖',
      rows: [
        ['Provider', s.value.llm.provider],
        ['模型', s.value.llm.model],
        ['接口地址', s.value.llm.base_url],
        ['API Key', s.value.llm.api_key_set ? '已配置' : '未配置'],
        ['温度 temperature', String(s.value.llm.temperature)],
        ['最大 tokens', String(s.value.llm.max_tokens)],
        ['超时 / 重试', `${s.value.llm.timeout_seconds}s / ${s.value.llm.max_retries} 次`],
        ...(s.value.llm.reasoning_enabled != null
          ? [['思考链 reasoning', s.value.llm.reasoning_enabled ? '开启' : '关闭'] as [string, string]]
          : []),
      ] as [string, string][],
      note: '模型切换属 P4-3（全局白名单），本期暂不开放编辑。',
    },
    {
      title: '检索与重排',
      icon: '🔎',
      rows: [
        ['召回条数 top_k', String(s.value.retrieval.search_top_k)],
        ['CrossEncoder 重排', s.value.retrieval.use_reranker ? '开启' : '关闭'],
        ...(s.value.retrieval.use_reranker
          ? ([
              ['重排模型', s.value.retrieval.reranker_model ?? '—'],
              ['候选池倍数', `top_k × ${s.value.retrieval.rerank_candidate_multiplier}`],
              [
                '重排分数阈值',
                s.value.retrieval.rerank_score_threshold != null
                  ? String(s.value.retrieval.rerank_score_threshold)
                  : '未启用',
              ],
            ] as [string, string][])
          : []),
      ] as [string, string][],
      note: '本卡的 top_k / 重排开关 / 阈值已可在上方「检索与重排」运行时开关里热改（此处为启动时快照）。',
    },
    {
      title: '向量库与嵌入',
      icon: '🧮',
      rows: [
        ['向量库类型', s.value.vector_store.type],
        ...(s.value.vector_store.collection_name
          ? [['Collection', s.value.vector_store.collection_name] as [string, string]]
          : []),
        ['持久化目录', s.value.vector_store.persist_directory],
        ['嵌入模型', s.value.embedding.model],
        ['设备 / 维度', `${s.value.embedding.device} / ${s.value.embedding.dimension ?? '—'} 维`],
        ...(s.value.vector_store.total_vectors != null
          ? [['片段总量', String(s.value.vector_store.total_vectors)] as [string, string]]
          : []),
      ] as [string, string][],
      note: '⚠️ 更换嵌入模型 = 更换向量空间 = 必须整库重建，禁止随意切换。',
    },
    {
      title: '文档切分',
      icon: '✂️',
      rows: [
        ['片段大小 chunk_size', `${s.value.chunking.chunk_size} 字符`],
        ['重叠 chunk_overlap', `${s.value.chunking.chunk_overlap} 字符`],
      ] as [string, string][],
      note: '⚠️ 更改切分参数 = 全库切片重排 = 必须重建索引（后续版本经确认按钮触发异步重建，暂不开放编辑）。',
    },
    {
      title: '会话记忆',
      icon: '💬',
      rows: [
        ['最大保留轮数', `${s.value.memory.max_turns} 轮`],
        ['会话过期时间', `${Math.round(s.value.memory.session_ttl_seconds / 3600)} 小时`],
      ] as [string, string][],
      note: '记忆轮数已可在上方「会话记忆」运行时开关里热改（此处为启动时快照）。',
    },
    {
      title: '意图识别',
      icon: '🧭',
      rows: [
        ['Provider', s.value.intent.provider],
        ['小模型', s.value.intent.model],
        ['超时', `${s.value.intent.timeout_seconds}s（超时降级规则匹配）`],
      ] as [string, string][],
    },
  ]
})

// --------------------------------------------------------------------------- //
// 运行时开关（P4-2）
// --------------------------------------------------------------------------- //
const flags = ref<RuntimeFlag[]>([])
const flagsLoading = ref(false)
const flagsError = ref('')
/** false = 非 superadmin（后端 403）→ 整块不渲染，页面保持纯只读 */
const canEdit = ref(false)
/** 草稿：key → 用户改过的值。与 f.value 不同即为「脏」，出现「确认修改」按钮 */
const drafts = reactive<Record<string, boolean | number>>({})
const savingKey = ref<string | null>(null)

/** 按注册表的 card 字段分组（卡片顺序 = 注册表里 key 字典序的稳定结果） */
const flagCards = computed(() => {
  const byCard = new Map<string, RuntimeFlag[]>()
  for (const f of flags.value) {
    const list = byCard.get(f.card) ?? []
    list.push(f)
    byCard.set(f.card, list)
  }
  return [...byCard.entries()].map(([card, items]) => ({ card, items }))
})

async function loadFlags() {
  flagsLoading.value = true
  flagsError.value = ''
  try {
    const res = await getRuntimeConfig()
    flags.value = res.flags
    canEdit.value = res.can_edit
    for (const f of res.flags) drafts[f.key] = f.value
  } catch (e) {
    // 403 是预期路径（非 superadmin），不是错误——静默退回只读模式
    if (e instanceof ApiError && e.status === 403) {
      canEdit.value = false
      flags.value = []
    } else {
      flagsError.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    flagsLoading.value = false
  }
}

function isDirty(f: RuntimeFlag): boolean {
  const d = drafts[f.key]
  return d !== undefined && d !== f.value
}

function toggleBool(f: RuntimeFlag) {
  drafts[f.key] = !drafts[f.key]
}

async function saveFlag(f: RuntimeFlag) {
  if (!isDirty(f) || savingKey.value) return
  // 数字输入可能拿到空串（v-model.number 对空输入给 ''）——先收敛成 number
  const raw = drafts[f.key]
  const payload = f.value_type === 'bool' ? Boolean(raw) : Number(raw)
  if (Number.isNaN(payload)) {
    ui.toast(`「${f.label}」的值不是有效数字`, 'error')
    return
  }
  savingKey.value = f.key
  try {
    const res = await patchRuntimeConfig(f.key, payload)
    f.value = res.value
    drafts[f.key] = res.value
    ui.toast(`「${f.label}」已保存，下一个请求即生效`, 'success')
  } catch (e) {
    ui.toast(e instanceof Error ? e.message : '保存失败', 'error')
  } finally {
    savingKey.value = null
  }
}
</script>

<template>
  <section class="settings-view">
    <header class="page-header">
      <div>
        <h2>系统设置</h2>
        <p class="page-desc">
          「运行时开关」修改**即时生效**（无需重启）；其余卡片来源于系统配置文件，修改需重启。
          运行时开关仅知识库超管（kb_role=superadmin）可改。
        </p>
      </div>
      <div class="header-actions">
        <button class="btn-ghost" :disabled="settingsStore.loading" @click="settingsStore.refresh(); loadFlags()">
          刷新
        </button>
        <button class="btn-primary" @click="ui.switchView('chat')">返回会话</button>
      </div>
    </header>

    <div v-if="settingsStore.loading" class="empty-state">读取配置中…</div>
    <div v-else-if="settingsStore.loadError" class="card error-card">
      读取配置失败：{{ settingsStore.loadError }}
      <button class="btn-primary" style="margin-left: 12px" @click="settingsStore.refresh()">重试</button>
    </div>

    <template v-else>
      <!-- ① 运行时开关（superadmin 专属；非超管 403 → 整块不渲染） -->
      <div v-if="flagsLoading" class="card group muted-note">读取运行时开关中…</div>
      <div v-else-if="flagsError" class="card error-card">
        运行时开关读取失败：{{ flagsError }}
        <button class="btn-primary" style="margin-left: 12px" @click="loadFlags()">重试</button>
      </div>
      <div v-else-if="flagCards.length" class="groups">
        <div v-for="c in flagCards" :key="c.card" class="card group">
          <h3 class="group-title">
            <span class="group-icon">🎛</span>{{ c.card }}
            <span class="hot-badge">即时生效</span>
          </h3>
          <div v-for="f in c.items" :key="f.key" class="flag-row">
            <div class="flag-info">
              <div class="flag-label">
                {{ f.label }}
                <code class="flag-key">{{ f.key }}</code>
              </div>
              <div class="flag-desc">{{ f.description }}</div>
            </div>
            <div class="flag-control">
              <template v-if="f.value_type === 'bool'">
                <button
                  class="toggle"
                  :class="{ on: Boolean(drafts[f.key]) }"
                  :disabled="savingKey === f.key"
                  :aria-label="f.label"
                  @click="toggleBool(f)"
                >
                  <span class="knob"></span>
                </button>
                <span class="flag-val">{{ drafts[f.key] ? '开启' : '关闭' }}</span>
              </template>
              <template v-else>
                <input
                  v-model.number="drafts[f.key]"
                  class="num-input"
                  type="number"
                  :step="f.value_type === 'float' ? '0.05' : '1'"
                  :min="f.min ?? undefined"
                  :max="f.max ?? undefined"
                  :disabled="savingKey === f.key"
                />
              </template>
              <button
                v-if="isDirty(f)"
                class="btn-primary btn-save"
                :disabled="savingKey === f.key"
                @click="saveFlag(f)"
              >
                {{ savingKey === f.key ? '保存中…' : '确认修改' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- ② 只读配置卡片 -->
      <div class="groups">
        <div v-for="g in groups" :key="g.title" class="card group">
          <h3 class="group-title"><span class="group-icon">{{ g.icon }}</span>{{ g.title }}</h3>
          <dl class="kv">
            <template v-for="[key, val] in g.rows" :key="key">
              <dt>{{ key }}</dt>
              <dd :class="{ mono: /目录|地址|Collection/.test(key) }" :title="val">{{ val }}</dd>
            </template>
          </dl>
          <p v-if="g.note" class="group-note">{{ g.note }}</p>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.settings-view {
  flex: 1;
  overflow-y: auto;
  padding: 20px 28px 32px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}
.page-header h2 {
  font-size: 17px;
}
.page-desc {
  margin-top: 4px;
  font-size: 12.5px;
  color: var(--text-3);
}
.header-actions {
  display: flex;
  gap: 10px;
  flex-shrink: 0;
}
.btn-ghost {
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-content);
  color: var(--text-2);
  font-size: 14px;
}
.btn-ghost:hover {
  background: var(--bg-hover);
}

.groups {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 14px;
}
.group {
  padding: 16px 18px;
}
.group-title {
  font-size: 14px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 10px;
}
.kv {
  display: grid;
  grid-template-columns: minmax(110px, auto) 1fr;
  row-gap: 8px;
  column-gap: 12px;
  font-size: 13px;
}
.kv dt {
  color: var(--text-3);
}
.kv dd {
  color: var(--text-1);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kv dd.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}
.group-note {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.6;
}
.error-card {
  padding: 18px;
  color: var(--danger);
  font-size: 13.5px;
  display: flex;
  align-items: center;
}
.muted-note {
  padding: 14px 18px;
  color: var(--text-3);
  font-size: 13px;
}

/* ---- 运行时开关（P4-2）---- */
.hot-badge {
  margin-left: auto;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--accent, #4a9eff);
  color: var(--accent, #4a9eff);
}
.flag-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 10px 0;
}
.flag-row + .flag-row {
  border-top: 1px dashed var(--border);
}
.flag-info {
  min-width: 0;
}
.flag-label {
  font-size: 13px;
  color: var(--text-1);
  display: flex;
  align-items: center;
  gap: 8px;
}
.flag-key {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  color: var(--text-3);
  background: var(--bg-hover);
  border-radius: 4px;
  padding: 1px 6px;
}
.flag-desc {
  margin-top: 3px;
  font-size: 12px;
  color: var(--text-3);
  line-height: 1.55;
}
.flag-control {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
  padding-top: 2px;
}
.flag-val {
  font-size: 12.5px;
  color: var(--text-2);
  width: 30px;
}
.toggle {
  width: 40px;
  height: 22px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-hover);
  position: relative;
  cursor: pointer;
  transition: background 0.15s ease;
  flex-shrink: 0;
}
.toggle .knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--text-3);
  transition: transform 0.15s ease, background 0.15s ease;
}
.toggle.on {
  background: var(--accent, #4a9eff);
  border-color: var(--accent, #4a9eff);
}
.toggle.on .knob {
  transform: translateX(18px);
  background: #fff;
}
.toggle:disabled {
  opacity: 0.6;
  cursor: wait;
}
.num-input {
  width: 84px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  background: var(--bg-content);
  color: var(--text-1);
  font-size: 13px;
}
.btn-save {
  padding: 6px 12px;
  font-size: 12.5px;
  white-space: nowrap;
}
</style>
