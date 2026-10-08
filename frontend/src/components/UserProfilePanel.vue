<script setup lang="ts">
/**
 * P2-16a/P2-20：个人中心面板（点侧边栏左下角用户区弹出）。
 *
 * P2-20 按用户要求参考 WorkBuddy 的交互重做：
 *  - **底部锚定的弹出菜单**（不是居中 Modal）：头像/名称在顶、
 *    菜单项在中间层（系统设置 / 文件传输·知识库 / 修改密码）、退出登录在底部；
 *  - 「系统设置」「文件传输」作为面板的菜单项（点击 → ui.switchView，面板关闭）；
 *    外侧个人名字右侧的退出登录入口**保留**，面板底部也有一份；
 *  - 基本资料与用量区保留（16a）。
 *
 * 改密表单（P2-18 保留 + P2-20 增强）：
 *  - 输入框有占位文本；
 *  - 🔴 新密码**实时校验**：输入时逐条显示 11b 规则是否满足（UX 预检）——
 *    判定的唯一出处仍是后端 `password_policy`（提交时后端会再完整验一遍），
 *    前端这层只是即时反馈，参数（最小长度）从 `/qa/me/password-policy` 下发；
 *  - 规则提示带**示例格式**（并注明仅演示、请勿直接使用）。
 *
 * 数据在打开时拉一次（低频查看动作，不轮询）。
 */
import { computed, ref, watch } from 'vue'
import {
  changeMyPassword,
  fetchMyProfile,
  fetchPasswordPolicyHint,
  type MyProfile,
  type PasswordPolicyHint,
} from '@/api/qa'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const auth = useAuthStore()
const ui = useUiStore()

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const profile = ref<MyProfile | null>(null)
const loading = ref(false)
const error = ref('')

// 视图切换：info（默认）⇄ password（改密表单）
const mode = ref<'info' | 'password'>('info')
const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const pwdError = ref('')
const pwdSaving = ref(false)
const policyHint = ref<PasswordPolicyHint | null>(null)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    loading.value = true
    error.value = ''
    profile.value = null
    // 每次打开重置到信息视图与表单
    mode.value = 'info'
    oldPwd.value = ''
    newPwd.value = ''
    newPwd2.value = ''
    pwdError.value = ''
    // P2-19：改密规则提示只在打开时拉一次（数字来自 11b 策略，前端不写死）
    fetchPasswordPolicyHint()
      .then((h) => (policyHint.value = h))
      .catch(() => (policyHint.value = null))
    try {
      profile.value = await fetchMyProfile()
    } catch {
      error.value = '个人信息加载失败，请稍后重试'
    } finally {
      loading.value = false
    }
  },
)

/** 示例密码：只演示「长位数 + 多字符类」的形状。⚠️ 注明请勿直接使用。 */
const SAMPLE_PASSWORD = 'Xk9#mQ2vLp'

/**
 * 🔴 实时校验（P2-20）：新密码输入时逐条显示 11b 规则是否满足。
 *
 * 规则与后端 `password_policy.validate_strength` 对齐：
 *   ① 长度 ≥ min_length（参数从 policy 端点下发，默认 10）
 *   ② 至少包含 大写/小写/数字/符号 中的两类
 *   ③ 不包含登录名（不区分大小写）
 * 两次输入一致单独一条（表单层规则）。
 *
 * ⚠️ 这是 **UX 预检**，不是判据第二份：后端提交时仍会完整校验。
 */
const pwdChecks = computed(() => {
  const v = newPwd.value
  const minLen = policyHint.value?.min_length ?? 10
  const classes = [
    /[a-z]/.test(v),
    /[A-Z]/.test(v),
    /\d/.test(v),
    /[^A-Za-z0-9]/.test(v),
  ].filter(Boolean).length
  const username = auth.username || ''
  return [
    {
      ok: v.length >= minLen,
      text: `至少 ${minLen} 位（当前 ${v.length}）`,
    },
    {
      ok: classes >= 2,
      text: '包含大写字母 / 小写字母 / 数字 / 符号中的至少两类',
    },
    {
      ok: username === '' || !v.toLowerCase().includes(username.toLowerCase()),
      text: '不包含登录名',
    },
    {
      ok: newPwd2.value !== '' && v === newPwd2.value,
      text: '两次输入一致',
    },
  ]
})

const allChecksPass = computed(() => pwdChecks.value.every((c) => c.ok))

function switchMode(m: 'info' | 'password') {
  mode.value = m
  pwdError.value = ''
  oldPwd.value = ''
  newPwd.value = ''
  newPwd2.value = ''
}

async function submitPassword() {
  pwdError.value = ''
  if (!allChecksPass.value) {
    pwdError.value = '新密码尚未满足全部规则'
    return
  }
  pwdSaving.value = true
  try {
    const res = await changeMyPassword(oldPwd.value, newPwd.value)
    // 成功 → 清凭据回登录页（token_version+1 后旧 token 必然 401，
    // 与其等下一次请求被打回，不如现在就干净地退出）
    window.alert(res.message || '密码已修改，请重新登录')
    await auth.logout()
  } catch (e) {
    pwdError.value = e instanceof Error && e.message ? e.message : '修改失败，请稍后重试'
  } finally {
    pwdSaving.value = false
  }
}

/** 菜单项：切视图 + 关面板 */
function navTo(view: 'settings' | 'knowledge') {
  ui.switchView(view)
  onClose()
}

function fmt(n: number | null | undefined): string {
  return (n ?? 0).toLocaleString('zh-CN')
}

function onClose() {
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <!-- 点击遮罩关闭；面板锚定在侧边栏左下角用户区上方（WorkBuddy 式） -->
    <div v-if="open" class="pp-mask" @click.self="onClose">
      <div class="pp-panel" role="dialog" aria-label="个人中心">
        <!-- ① 头部：头像 + 姓名 + 部门职位 -->
        <header class="pp-card pp-head-card">
          <div class="pp-avatar">{{ profile?.profile.display_name.slice(0, 1) ?? '…' }}</div>
          <div class="pp-head-text">
            <p class="pp-name">{{ profile?.profile.display_name ?? '—' }}</p>
            <p class="pp-sub muted">
              {{ profile?.profile.department ?? '未分配部门' }} · {{ profile?.profile.position ?? '未分配职位' }}
            </p>
          </div>
        </header>

        <p v-if="loading" class="pp-state">加载中…</p>
        <p v-else-if="error" class="pp-state pp-error">{{ error }}</p>

        <!-- ② 改密视图（面板内切换） -->
        <div v-else-if="mode === 'password'" class="pp-card pp-pwd-card">
          <div class="pp-card-title">
            <button class="pp-back" aria-label="返回" @click="switchMode('info')">‹</button>
            <span>修改密码</span>
          </div>
          <label class="pp-field">
            <span>当前密码</span>
            <input
              v-model="oldPwd"
              type="password"
              placeholder="输入当前使用的密码"
              autocomplete="current-password"
            />
          </label>
          <label class="pp-field">
            <span>新密码</span>
            <input
              v-model="newPwd"
              type="password"
              placeholder="输入新密码（见下方规则）"
              autocomplete="new-password"
            />
          </label>
          <!-- 🔴 实时校验：输入时逐条显示规则是否满足 -->
          <ul v-if="newPwd" class="pp-checks">
            <li v-for="c in pwdChecks" :key="c.text" :class="{ ok: c.ok }">
              <span class="pp-check-mark">{{ c.ok ? '✓' : '○' }}</span>
              {{ c.text }}
            </li>
          </ul>
          <label class="pp-field">
            <span>确认新密码</span>
            <input
              v-model="newPwd2"
              type="password"
              placeholder="再输入一次新密码"
              autocomplete="new-password"
            />
          </label>
          <p v-if="pwdError" class="pp-error">{{ pwdError }}</p>
          <p v-if="policyHint" class="pp-rules">
            规则：至少 {{ policyHint.min_length }} 位，含大小写字母、数字、符号中至少两类，
            不含登录名，不与最近 {{ policyHint.history_keep }} 次重复。
            示例格式：<code class="mono">{{ SAMPLE_PASSWORD }}</code>
            （仅演示形状，请勿直接使用）。有效期 {{ policyHint.expire_days }} 天。
          </p>
          <button
            class="pp-btn pp-btn--primary pp-btn--block"
            :disabled="pwdSaving || !allChecksPass || !oldPwd"
            @click="submitPassword"
          >
            {{ pwdSaving ? '提交中…' : '确认修改' }}
          </button>
          <p class="pp-note muted">改密成功后会退出登录，请用新密码重新登录。</p>
        </div>

        <!-- ③ 信息视图（默认） -->
        <template v-else-if="profile">
          <div class="pp-card">
            <dl class="pp-rows">
              <div class="pp-row"><dt>登录名</dt><dd class="mono">{{ profile.profile.username }}</dd></div>
              <div class="pp-row"><dt>工号</dt><dd class="mono">{{ profile.profile.employee_no }}</dd></div>
              <div class="pp-row"><dt>角色</dt><dd>{{ profile.profile.role_label }}</dd></div>
              <div class="pp-row"><dt>邮箱</dt><dd>{{ profile.profile.email ?? '—' }}</dd></div>
              <div class="pp-row"><dt>手机</dt><dd class="mono">{{ profile.profile.phone ?? '—' }}</dd></div>
              <div class="pp-row"><dt>入职</dt><dd>{{ profile.profile.joined_at.slice(0, 10) }}</dd></div>
            </dl>
          </div>

          <div class="pp-card">
            <div class="pp-usage-line">
              <span class="pp-usage-label">本月用量</span>
              <span class="mono">{{ fmt(profile.month_usage.billable_tokens) }}</span>
            </div>
            <div class="pp-usage-line">
              <span class="pp-usage-label">月度额度</span>
              <span class="mono">
                {{ profile.month_usage.effective_quota > 0 ? fmt(profile.month_usage.effective_quota) : '不限' }}
                <template v-if="profile.month_usage.usage_percent != null">
                  （{{ profile.month_usage.usage_percent.toFixed(1) }}%）
                </template>
              </span>
            </div>
            <div class="pp-usage-line">
              <span class="pp-usage-label">历史总用量</span>
              <span class="mono">{{ fmt(profile.total_usage.billable_tokens) }} · {{ profile.total_usage.requests }} 次</span>
            </div>
            <p class="pp-note muted">超额只提醒，不限制使用；删除会话会同时移除其用量记录。</p>
          </div>

          <!-- ④ 菜单层（中间层）：系统设置 / 文件传输 / 修改密码 -->
          <nav class="pp-menu">
            <button class="pp-menu-item" @click="navTo('settings')">
              <span class="pp-menu-icon">⚙</span>
              <span>系统设置</span>
              <span class="pp-menu-arrow">›</span>
            </button>
            <button class="pp-menu-item" @click="navTo('knowledge')">
              <span class="pp-menu-icon">⇪</span>
              <span>文件传输 · 知识库</span>
              <span class="pp-menu-arrow">›</span>
            </button>
            <button class="pp-menu-item" @click="switchMode('password')">
              <span class="pp-menu-icon">🔑</span>
              <span>修改密码</span>
              <span class="pp-menu-arrow">›</span>
            </button>
          </nav>

          <!-- ⑤ 底部：退出登录（外侧个人名字右侧的退出入口保留） -->
          <div class="pp-menu pp-menu--last">
            <button class="pp-menu-item pp-logout" @click="auth.logout()">
              <span class="pp-menu-icon">→</span>
              <span>退出登录</span>
            </button>
          </div>
        </template>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 遮罩：透明（点击空白关闭），不用暗遮罩 —— 面板是轻量弹出，不是模态 */
.pp-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
}

/* 🔴 底部锚定：面板贴着侧边栏左下角用户区的上方（WorkBuddy 式交互）。
   侧边栏宽 --sidebar-width，用户区高约 60px —— 面板 left/bottom 与之对齐。 */
.pp-panel {
  position: fixed;
  left: 10px;
  bottom: 74px;
  width: calc(var(--sidebar-width, 260px) - 20px);
  max-height: calc(100vh - 100px);
  overflow-y: auto;
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e5e5);
  border-radius: 14px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.18);
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pp-card {
  background: var(--bg-app, rgba(0, 0, 0, 0.02));
  border-radius: 10px;
  padding: 12px;
}

.pp-head-card {
  display: flex;
  align-items: center;
  gap: 12px;
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
  flex-shrink: 0;
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
  gap: 5px;
}

.pp-row {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
}

.pp-row dt {
  color: var(--text-3, #888);
}

.pp-row dd {
  margin: 0;
}

.pp-usage-line {
  display: flex;
  justify-content: space-between;
  font-size: 13px;
  padding: 3px 0;
}

.pp-usage-label {
  color: var(--text-3, #888);
}

.pp-menu {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.pp-menu--last {
  border-top: 1px solid var(--border, #eee);
  padding-top: 6px;
}

.pp-menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  border: none;
  background: none;
  color: inherit;
  font-size: 13px;
  padding: 9px 10px;
  border-radius: 8px;
  cursor: pointer;
  text-align: left;
}

.pp-menu-item:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.05));
}

.pp-menu-icon {
  width: 18px;
  text-align: center;
  color: var(--text-2, #666);
}

.pp-menu-arrow {
  margin-left: auto;
  color: var(--text-3, #999);
}

.pp-logout {
  color: #d9534f;
}

.pp-logout .pp-menu-icon {
  color: #d9534f;
}

/* 改密表单 */
.pp-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 10px;
}

.pp-back {
  border: none;
  background: none;
  font-size: 18px;
  cursor: pointer;
  color: var(--text-2, #666);
  padding: 0 4px;
}

.pp-pwd-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.pp-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.pp-field span {
  font-size: 12px;
  color: var(--text-3, #888);
}

.pp-field input {
  padding: 8px 10px;
  border: 1px solid var(--border, #ddd);
  border-radius: 8px;
  background: var(--bg-app, #fff);
  color: inherit;
  font-size: 13px;
}

.pp-field input:focus {
  outline: none;
  border-color: var(--primary, #4a6cf7);
}

/* 🔴 实时校验列表：满足=绿勾，未满足=灰圈 */
.pp-checks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.pp-checks li {
  font-size: 12px;
  color: var(--text-3, #999);
  display: flex;
  align-items: center;
  gap: 6px;
}

.pp-checks li.ok {
  color: #3fb950;
}

.pp-check-mark {
  width: 14px;
  text-align: center;
}

.pp-rules {
  font-size: 12px;
  color: var(--text-2, #666);
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  border-radius: 8px;
  padding: 8px 10px;
  margin: 0;
  line-height: 1.6;
}

.pp-btn {
  padding: 8px 14px;
  border-radius: 8px;
  border: 1px solid var(--border, #ddd);
  background: none;
  color: inherit;
  font-size: 13px;
  cursor: pointer;
}

.pp-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.pp-btn--primary {
  background: var(--primary, #4a6cf7);
  border-color: var(--primary, #4a6cf7);
  color: #fff;
}

.pp-btn--block {
  width: 100%;
}

.pp-note {
  font-size: 11px;
  margin: 0;
}

.pp-state {
  text-align: center;
  color: var(--text-3, #888);
  padding: 24px 0;
}

.pp-error {
  color: #d9534f;
  font-size: 12px;
  margin: 0;
}

.muted {
  color: var(--text-3, #888);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
