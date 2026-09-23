<script setup lang="ts">
/**
 * 登录页 —— 系统唯一入口。
 *
 * 设计取舍：
 * 1. 不做「记住我」勾选框：token 本来就落 localStorage（刷新/新标签页都保持登录），
 *    加一个勾选框只会让人误以为「不勾就会很快登出」，实际两者没有区别。
 * 2. 错误只显示一条（最近一次），不做错误列表：登录失败的原因对用户而言
 *    没有多种，给多了反而像系统在指责用户。
 * 3. 表单提交走 <form> 的 submit 事件而不是按钮 click：
 *    这样回车能直接提交（浏览器的原生能力，不用手动监听 keydown）。
 */
import { computed, ref } from 'vue'
import { ApiError } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/** 仅开发构建下展示默认账号提示（生产构建里该分支会被摇掉） */
const isDev = import.meta.env.DEV

const username = ref('')
const password = ref('')
const errorText = ref('')
const loading = ref(false)

const canSubmit = computed(
  () => username.value.trim() !== '' && password.value !== '' && !loading.value,
)

async function submit() {
  if (!canSubmit.value) return
  loading.value = true
  errorText.value = ''
  try {
    await auth.login(username.value.trim(), password.value)
    // 登录成功后清掉密码框：凭据不该在内存/表单里多留一秒
    password.value = ''
  } catch (e) {
    errorText.value =
      e instanceof ApiError
        ? e.status === 401
          ? '用户名或密码错误'
          : e.message
        : '登录失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <div class="brand">
        <div class="brand-mark">RAG</div>
        <h1>企业级 RAG 智能问答系统</h1>
        <p>请登录后使用</p>
      </div>

      <!-- 被动登出的说明：用户没有预期会被踢下线，必须给出原因 -->
      <p v-if="auth.kickedReason" class="notice">{{ auth.kickedReason }}</p>

      <label class="field">
        <span>用户名</span>
        <input
          v-model="username"
          type="text"
          autocomplete="username"
          placeholder="请输入用户名"
          :disabled="loading"
        />
      </label>

      <label class="field">
        <span>密码</span>
        <input
          v-model="password"
          type="password"
          autocomplete="current-password"
          placeholder="请输入密码"
          :disabled="loading"
        />
      </label>

      <p v-if="errorText" class="error">{{ errorText }}</p>

      <button class="submit" type="submit" :disabled="!canSubmit">
        {{ loading ? '登录中…' : '登录' }}
      </button>

      <p v-if="isDev" class="hint">本地开发默认账号：admin / admin123</p>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #f5f6f8 0%, #eef1fe 100%);
}

.login-card {
  width: 380px;
  padding: 36px 32px 28px;
  background: var(--bg-content);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: 0 12px 32px rgba(31, 35, 41, 0.08);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.brand {
  text-align: center;
  margin-bottom: 4px;
}

.brand-mark {
  width: 48px;
  height: 48px;
  margin: 0 auto 12px;
  border-radius: 12px;
  background: var(--primary);
  color: #fff;
  font-weight: 700;
  font-size: 15px;
  letter-spacing: 0.5px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand h1 {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
}

.brand p {
  margin-top: 6px;
  font-size: 13px;
  color: var(--text-3);
}

.notice {
  padding: 9px 12px;
  border-radius: var(--radius-sm);
  background: var(--primary-light);
  color: var(--primary);
  font-size: 13px;
  line-height: 1.5;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field span {
  font-size: 13px;
  color: var(--text-2);
}

.field input {
  height: 38px;
  padding: 0 12px;
  font-size: 14px;
  font-family: inherit;
  color: var(--text-1);
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  outline: none;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.field input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-light);
}

.field input:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.error {
  font-size: 13px;
  color: var(--danger);
  background: var(--danger-light);
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  line-height: 1.5;
}

.submit {
  height: 40px;
  margin-top: 4px;
  border-radius: var(--radius-sm);
  background: var(--primary);
  color: #fff;
  font-size: 14px;
  font-weight: 500;
  transition: background 0.15s;
}

.submit:hover:not(:disabled) {
  background: var(--primary-hover);
}

.submit:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.hint {
  text-align: center;
  font-size: 12px;
  color: var(--text-3);
}
</style>
