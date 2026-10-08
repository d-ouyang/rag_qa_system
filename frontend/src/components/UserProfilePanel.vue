<script setup lang="ts">
/**
 * P2-16a：个人信息面板（点侧边栏左下角用户区弹出）。
 *
 * 三个刻意边界：
 * 1. **只读**。这里能看的都是事实（姓名/部门/用量），没有可改的字段 ——
 *    改资料的入口在管理端（人事维护），问答端给编辑入口等于把人事数据
 *    的真相源从 MySQL 挪到「用户自己随手改」。
 * 2. **用量数字全部后端算好**（当月 + 历史总用量，口径见 /qa/me/profile）。
 *    前端只渲染 —— 「同一字段两个口径」的教训不在这里重犯。
 * 3. **历史总用量的口径是「现存明细的累计」**：会话被删会随之变小
 *    （删除是硬删）。文案因此写「历史总用量」而不写「累计消耗」，
 *    面板副标题注明，免得有人较真「我明明问过更多」。
 *
 * 数据在**打开时**拉一次（不是常驻轮询）：面板是低频查看动作，
 * 打开时最新即可；后台放着不动时数字过期无妨。
 */
import { ref, watch } from 'vue'
import { fetchMyProfile, type MyProfile } from '@/api/qa'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const profile = ref<MyProfile | null>(null)
const loading = ref(false)
const error = ref('')

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    loading.value = true
    error.value = ''
    profile.value = null
    try {
      profile.value = await fetchMyProfile()
    } catch {
      error.value = '个人信息加载失败，请稍后重试'
    } finally {
      loading.value = false
    }
  },
)

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('zh-CN')
}

function onClose() {
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="profile-mask" @click.self="onClose">
      <div class="profile-panel" role="dialog" aria-label="个人信息">
        <header class="pp-head">
          <h2>个人信息</h2>
          <button class="pp-close" aria-label="关闭" @click="onClose">×</button>
        </header>

        <p v-if="loading" class="pp-state">加载中…</p>
        <p v-else-if="error" class="pp-state pp-error">{{ error }}</p>

        <template v-else-if="profile">
          <!-- 基本信息 -->
          <section class="pp-section">
            <div class="pp-id">
              <div class="pp-avatar">{{ profile.profile.display_name.slice(0, 1) }}</div>
              <div>
                <p class="pp-name">{{ profile.profile.display_name }}</p>
                <p class="pp-sub muted">{{ profile.profile.department ?? '未分配部门' }} · {{ profile.profile.position ?? '未分配职位' }}</p>
              </div>
            </div>
            <dl class="pp-rows">
              <div class="pp-row"><dt>登录名</dt><dd class="mono">{{ profile.profile.username }}</dd></div>
              <div class="pp-row"><dt>工号</dt><dd class="mono">{{ profile.profile.employee_no }}</dd></div>
              <div class="pp-row"><dt>角色</dt><dd>{{ profile.profile.role_label }}</dd></div>
              <div class="pp-row"><dt>邮箱</dt><dd>{{ profile.profile.email ?? '—' }}</dd></div>
              <div class="pp-row"><dt>手机</dt><dd class="mono">{{ profile.profile.phone ?? '—' }}</dd></div>
              <div class="pp-row"><dt>入职时间</dt><dd>{{ profile.profile.joined_at.slice(0, 10) }}</dd></div>
            </dl>
          </section>

          <!-- 本月用量 -->
          <section class="pp-section">
            <h3 class="pp-title">
              本月用量
              <span class="badge" :class="`pp-badge--${profile.month_usage.status}`">
                {{ profile.month_usage.status_label }}
              </span>
            </h3>
            <div class="pp-usage">
              <div class="pp-usage-item">
                <span class="pp-usage-num mono">{{ fmt(profile.month_usage.billable_tokens) }}</span>
                <span class="pp-usage-label">计费 tokens</span>
              </div>
              <div class="pp-usage-item">
                <span class="pp-usage-num mono">{{ fmt(profile.month_usage.input_tokens) }}</span>
                <span class="pp-usage-label">输入</span>
              </div>
              <div class="pp-usage-item">
                <span class="pp-usage-num mono">{{ fmt(profile.month_usage.output_tokens) }}</span>
                <span class="pp-usage-label">输出</span>
              </div>
            </div>
            <p class="pp-quota muted">
              月度额度 {{ fmt(profile.month_usage.effective_quota) }} tokens
              （{{ profile.month_usage.usage_percent == null ? '—' : profile.month_usage.usage_percent.toFixed(1) + '%' }}）
              · 超额只提醒，不限制使用
            </p>
          </section>

          <!-- 历史总用量 -->
          <section class="pp-section">
            <h3 class="pp-title">历史总用量</h3>
            <div class="pp-usage">
              <div class="pp-usage-item">
                <span class="pp-usage-num mono">{{ fmt(profile.total_usage.billable_tokens) }}</span>
                <span class="pp-usage-label">计费 tokens</span>
              </div>
              <div class="pp-usage-item">
                <span class="pp-usage-num mono">{{ fmt(profile.total_usage.requests) }}</span>
                <span class="pp-usage-label">问答次数</span>
              </div>
            </div>
            <p class="muted pp-note">按现存会话记录累计；删除会话会同时移除其用量记录。</p>
          </section>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.profile-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 60;
}

.profile-panel {
  width: 400px;
  max-height: 82vh;
  overflow-y: auto;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px;
}

.pp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.pp-head h2 {
  margin: 0;
  font-size: 16px;
}

.pp-close {
  border: none;
  background: none;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  color: var(--text-3, #888);
  padding: 2px 6px;
}

.pp-state {
  text-align: center;
  color: var(--text-3, #888);
  padding: 24px 0;
}

.pp-error {
  color: #d9534f;
}

.pp-section {
  padding: 12px 0;
  border-top: 1px solid var(--border, #eee);
}

.pp-id {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}

.pp-avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--primary, #4a6cf7);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: 600;
}

.pp-name {
  margin: 0;
  font-weight: 600;
  font-size: 15px;
}

.pp-sub {
  margin: 2px 0 0;
  font-size: 12px;
}

.pp-rows {
  margin: 0;
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
}

.pp-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  padding: 2px 0;
}

.pp-row dt {
  color: var(--text-3, #888);
}

.pp-row dd {
  margin: 0;
}

.pp-title {
  margin: 0 0 10px;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
}

.pp-usage {
  display: flex;
  gap: 10px;
}

.pp-usage-item {
  flex: 1;
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  border-radius: 8px;
  padding: 10px;
  text-align: center;
}

.pp-usage-num {
  display: block;
  font-size: 15px;
  font-weight: 600;
}

.pp-usage-label {
  display: block;
  font-size: 11px;
  color: var(--text-3, #888);
  margin-top: 2px;
}

.pp-quota {
  font-size: 12px;
  margin: 8px 0 0;
}

.pp-note {
  font-size: 11px;
  margin: 8px 0 0;
}

.pp-badge--ok {
  background: rgba(63, 185, 80, 0.15);
  color: #3fb950;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
}

.pp-badge--warn {
  background: rgba(240, 177, 60, 0.15);
  color: #d9a13c;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
}

.pp-badge--over {
  background: rgba(240, 97, 109, 0.15);
  color: #e5737d;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
}

.muted {
  color: var(--text-3, #888);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
