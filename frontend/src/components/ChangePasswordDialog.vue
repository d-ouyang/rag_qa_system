<script setup lang="ts">
/**
 * P2-21：修改密码——**独立弹窗**（从个人中心面板里拆出来）。
 *
 * 为什么拆：面板是「看信息 + 导航」的轻量弹出，改密是「填表 + 校验 + 提交」
 * 的完整任务 —— 两者放一起会让面板承担两种交互模型，而且改密成功要退出登录，
 * 把「登出」这个副作用挂在面板里会让面板的生命周期变得难懂。
 *
 * 内容与之前面板里的表单一致（占位文本 / 实时校验 / 示例格式）；
 * 🔴 判定的唯一出处仍是后端 `password_policy`，这里是 UX 预检（提交时后端再验）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { changeMyPassword, fetchPasswordPolicyHint, type PasswordPolicyHint } from '@/api/qa'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const auth = useAuthStore()

const oldPwd = ref('')
const newPwd = ref('')
const newPwd2 = ref('')
const pwdError = ref('')
const pwdSaving = ref(false)
const policyHint = ref<PasswordPolicyHint | null>(null)

watch(
  () => props.open,
  (open) => {
    if (!open) return
    oldPwd.value = ''
    newPwd.value = ''
    newPwd2.value = ''
    pwdError.value = ''
  },
)

onMounted(() => {
  fetchPasswordPolicyHint()
    .then((h) => (policyHint.value = h))
    .catch(() => (policyHint.value = null))
})

/** 示例密码：只演示「长位数 + 多字符类」的形状。⚠️ 注明请勿直接使用。 */
const SAMPLE_PASSWORD = 'Xk9#mQ2vLp'

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
    { ok: v.length >= minLen, text: `至少 ${minLen} 位（当前 ${v.length}）` },
    { ok: classes >= 2, text: '包含大写字母 / 小写字母 / 数字 / 符号中的至少两类' },
    { ok: username === '' || !v.toLowerCase().includes(username.toLowerCase()), text: '不包含登录名' },
    { ok: newPwd2.value !== '' && v === newPwd2.value, text: '两次输入一致' },
  ]
})

const allChecksPass = computed(() => pwdChecks.value.every((c) => c.ok))

async function submit() {
  pwdError.value = ''
  if (!allChecksPass.value) {
    pwdError.value = '新密码尚未满足全部规则'
    return
  }
  pwdSaving.value = true
  try {
    const res = await changeMyPassword(oldPwd.value, newPwd.value)
    window.alert(res.message || '密码已修改，请重新登录')
    emit('close')
    // token_version+1 后旧 token 必然 401 —— 干净地退出，
    // 不等下一次请求被打回（登出后 App.vue 的登录态分流会渲染登录页）
    await auth.logout()
  } catch (e) {
    pwdError.value = e instanceof Error && e.message ? e.message : '修改失败，请稍后重试'
  } finally {
    pwdSaving.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cp-mask" @click.self="emit('close')">
      <div class="cp-panel" role="dialog" aria-label="修改密码">
        <h3 class="cp-title">修改密码</h3>
        <label class="cp-field">
          <span>当前密码</span>
          <input
            v-model="oldPwd"
            type="password"
            placeholder="输入当前使用的密码"
            autocomplete="current-password"
          />
        </label>
        <label class="cp-field">
          <span>新密码</span>
          <input
            v-model="newPwd"
            type="password"
            placeholder="输入新密码（见下方规则）"
            autocomplete="new-password"
          />
        </label>
        <!-- 🔴 实时校验：输入时逐条显示规则是否满足 -->
        <ul v-if="newPwd" class="cp-checks">
          <li v-for="c in pwdChecks" :key="c.text" :class="{ ok: c.ok }">
            <span class="cp-check-mark">{{ c.ok ? '✓' : '○' }}</span>
            {{ c.text }}
          </li>
        </ul>
        <label class="cp-field">
          <span>确认新密码</span>
          <input
            v-model="newPwd2"
            type="password"
            placeholder="再输入一次新密码"
            autocomplete="new-password"
          />
        </label>
        <p v-if="pwdError" class="cp-error">{{ pwdError }}</p>
        <p v-if="policyHint" class="cp-rules">
          规则：至少 {{ policyHint.min_length }} 位，含大小写字母、数字、符号中至少两类，
          不含登录名，不与最近 {{ policyHint.history_keep }} 次重复。
          示例格式：<code class="mono">{{ SAMPLE_PASSWORD }}</code>
          （仅演示形状，请勿直接使用）。有效期 {{ policyHint.expire_days }} 天。
        </p>
        <div class="cp-actions">
          <button class="cp-btn" @click="emit('close')">取消</button>
          <button
            class="cp-btn cp-btn--primary"
            :disabled="pwdSaving || !allChecksPass || !oldPwd"
            @click="submit"
          >
            {{ pwdSaving ? '提交中…' : '确认修改' }}
          </button>
        </div>
        <p class="cp-note muted">改密成功后会退出登录，请用新密码重新登录。</p>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cp-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 70;
}

.cp-panel {
  width: 420px;
  max-width: calc(100vw - 40px);
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e5e5);
  border-radius: 12px;
  padding: 22px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.24);
}

.cp-title {
  margin: 0 0 4px;
  font-size: 16px;
  font-weight: 600;
}

.cp-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cp-field span {
  font-size: 12px;
  color: var(--text-3, #888);
}

.cp-field input {
  padding: 9px 11px;
  border: 1px solid var(--border, #ddd);
  border-radius: 8px;
  background: var(--bg-app, #fff);
  color: inherit;
  font-size: 13px;
}

.cp-field input:focus {
  outline: none;
  border-color: var(--primary, #4a6cf7);
}

.cp-checks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.cp-checks li {
  font-size: 12px;
  color: var(--text-3, #999);
  display: flex;
  align-items: center;
  gap: 6px;
}

.cp-checks li.ok {
  color: #3fb950;
}

.cp-check-mark {
  width: 14px;
  text-align: center;
}

.cp-rules {
  font-size: 12px;
  color: var(--text-2, #666);
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
  border-radius: 8px;
  padding: 8px 10px;
  margin: 0;
  line-height: 1.6;
}

.cp-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 4px;
}

.cp-btn {
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid var(--border, #ddd);
  background: none;
  color: inherit;
  font-size: 13px;
  cursor: pointer;
}

.cp-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.cp-btn--primary {
  background: var(--primary, #4a6cf7);
  border-color: var(--primary, #4a6cf7);
  color: #fff;
}

.cp-error {
  color: #d9534f;
  font-size: 12px;
  margin: 0;
}

.cp-note {
  font-size: 11px;
  margin: 0;
}

.muted {
  color: var(--text-3, #888);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
</style>
