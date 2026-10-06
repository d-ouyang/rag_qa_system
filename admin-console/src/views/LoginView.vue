<script setup lang="ts">
/**
 * 管理端登录页。
 *
 * 与主应用登录页的三处不同，每一处都有理由：
 *
 * 1. **登录失败 ≠ 密码错误。** 这里要区分「账号密码错」（网关 401）与
 *    「账号没权限 / 已停用」（管理端 403 或停用）。后者提示必须是
 *    「你的账号没有管理端权限」，否则用户会去重试密码，直到被锁 15 分钟。
 * 2. **不提示默认账号。** 主应用会在 dev 下显示 admin/admin123；能进这里的人
 *    管着别人的密码，把账号名写在登录页上不合适。
 * 3. **登录成功后不做任何「记住」动作** —— 由 store 内部的
 *    `/api/v1/admin/me` 二次确认后再放行。
 */
import { computed, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

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
    // 凭据不该在表单里多留一秒
    password.value = ''
  } catch (e) {
    errorText.value = e instanceof Error ? e.message : '登录失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <div class="brand">
        <div class="brand-mark">RA</div>
        <h1>RAG 管理端</h1>
        <p class="muted">员工 · 组织 · 密码</p>
      </div>

      <label class="field">
        <span>登录名</span>
        <input
          v-model="username"
          class="input"
          type="text"
          autocomplete="username"
          placeholder="管理员或人事账号"
          autofocus
        />
      </label>

      <label class="field">
        <span>密码</span>
        <input
          v-model="password"
          class="input"
          type="password"
          autocomplete="current-password"
          placeholder="请输入密码"
        />
      </label>

      <p v-if="errorText" class="error">{{ errorText }}</p>
      <p v-else-if="auth.kickedReason" class="notice">{{ auth.kickedReason }}</p>

      <button class="btn btn-primary submit" type="submit" :disabled="!canSubmit">
        {{ loading ? '正在登录…' : '登录' }}
      </button>

      <p class="foot muted">
        登录走鉴权网关（与主应用同一个入口）；进入后还会向后端确认你的角色，
        <strong>普通员工账号会被拒</strong>。
      </p>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(900px 480px at 50% -10%, rgba(79, 110, 247, 0.14), transparent 60%),
    var(--bg-app);
  padding: 24px;
}
.login-card {
  width: 100%;
  max-width: 380px;
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-pop);
  padding: 28px 26px 22px;
}
.brand {
  text-align: center;
  margin-bottom: 22px;
}
.brand-mark {
  width: 42px;
  height: 42px;
  margin: 0 auto 12px;
  border-radius: 11px;
  background: var(--primary);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 700;
}
.brand h1 {
  font-size: 19px;
  font-weight: 600;
}
.brand p {
  font-size: 13px;
  margin-top: 4px;
}
.error {
  background: var(--danger-soft);
  border: 1px solid rgba(240, 97, 109, 0.32);
  color: var(--danger);
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  margin-bottom: 14px;
  line-height: 1.5;
}
.notice {
  background: var(--warn-soft);
  border: 1px solid rgba(240, 177, 60, 0.3);
  color: var(--warn);
  padding: 9px 11px;
  border-radius: var(--radius-sm);
  font-size: 13px;
  margin-bottom: 14px;
}
.submit {
  width: 100%;
  justify-content: center;
  padding: 10px;
}
.foot {
  margin-top: 16px;
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
}
</style>
