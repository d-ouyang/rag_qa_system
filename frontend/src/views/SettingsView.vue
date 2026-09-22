<script setup lang="ts">
/**
 * 系统设置页（右侧内容区 · settings 视图）。
 * 只读展示当前问答系统关键配置项（分组卡片），
 * 配置来源 .env + 环境变量，修改后需重启后端生效。
 */
import { computed, onMounted } from 'vue'
import { useSettingsStore } from '@/stores/settings'
import { useUiStore } from '@/stores/ui'

const settingsStore = useSettingsStore()
const ui = useUiStore()

onMounted(() => void settingsStore.refresh())

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
    },
    {
      title: '文档切分',
      icon: '✂️',
      rows: [
        ['片段大小 chunk_size', `${s.value.chunking.chunk_size} 字符`],
        ['重叠 chunk_overlap', `${s.value.chunking.chunk_overlap} 字符`],
      ] as [string, string][],
    },
    {
      title: '会话记忆',
      icon: '💬',
      rows: [
        ['最大保留轮数', `${s.value.memory.max_turns} 轮`],
        ['会话过期时间', `${Math.round(s.value.memory.session_ttl_seconds / 3600)} 小时`],
      ] as [string, string][],
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
</script>

<template>
  <section class="settings-view">
    <header class="page-header">
      <div>
        <h2>系统设置</h2>
        <p class="page-desc">当前问答系统运行配置（只读）。配置来源于 .env 与环境变量，修改后重启后端生效。</p>
      </div>
      <div class="header-actions">
        <button class="btn-ghost" :disabled="settingsStore.loading" @click="settingsStore.refresh()">
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

    <div v-else class="groups">
      <div v-for="g in groups" :key="g.title" class="card group">
        <h3 class="group-title"><span class="group-icon">{{ g.icon }}</span>{{ g.title }}</h3>
        <dl class="kv">
          <template v-for="[key, val] in g.rows" :key="key">
            <dt>{{ key }}</dt>
            <dd :class="{ mono: /目录|地址|Collection/.test(key) }" :title="val">{{ val }}</dd>
          </template>
        </dl>
      </div>
    </div>
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
.error-card {
  padding: 18px;
  color: var(--danger);
  font-size: 13.5px;
  display: flex;
  align-items: center;
}
</style>
