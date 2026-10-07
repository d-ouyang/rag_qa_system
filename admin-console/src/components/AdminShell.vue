/**
 * 管理端外壳 —— 左侧导航 + 右侧内容。
 *
 * 路由之外刻意保留的一件事：**顶栏上永远显示「当前登录者是谁」**。
 * 管理端的操作会改动别人 —— 重置密码、停用账号 —— 事后第一个被问到的
 * 问题永远是「是谁干的」。让操作者身份常驻可见，是给审计（P2-13d）先垫一步。
 */
<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { ROLE_LABEL } from '@/api/admin'

interface NavItem {
  to: string
  label: string
  icon: string
}

const NAV: NavItem[] = [
  { to: '/overview', label: '概览', icon: '◲' },
  { to: '/users', label: '员工', icon: '☰' },
  { to: '/org', label: '部门与职位', icon: '⑃' },
  { to: '/passwords', label: '密码管理', icon: '⚿' },
  { to: '/audit', label: '审计日志', icon: '☷' },
]

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const roleLabel = computed(() => {
  const profile = auth.profile
  return profile ? ROLE_LABEL[profile.role] ?? profile.role : ''
})

async function logout() {
  await auth.logout()
  void router.push({ name: 'login' })
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand-mark">RA</div>
        <div class="brand-text">
          <strong>RAG 管理端</strong>
          <span class="muted">员工 · 组织 · 密码</span>
        </div>
      </div>

      <nav class="nav">
        <RouterLink
          v-for="item in NAV"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ active: route.path.startsWith(item.to) }"
        >
          <span class="nav-icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-foot">
        <div class="who">
          <div class="avatar">{{ (auth.profile?.display_name ?? '?').slice(0, 1) }}</div>
          <div class="who-text">
            <strong>{{ auth.profile?.display_name ?? auth.username }}</strong>
            <span class="muted">{{ roleLabel }}</span>
          </div>
        </div>
        <button class="btn btn-ghost btn-sm logout" @click="logout">退出登录</button>
      </div>
    </aside>

    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  height: 100%;
}
.sidebar {
  width: var(--sidebar-width);
  flex: 0 0 var(--sidebar-width);
  background: var(--bg-panel);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid var(--border);
}
.brand-mark {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 700;
  font-size: 13px;
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}
.brand-text .muted {
  font-size: 12px;
}
.nav {
  flex: 1;
  padding: 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border-radius: var(--radius-sm);
  color: var(--text-2);
  transition:
    background 0.15s,
    color 0.15s;
}
.nav-item:hover {
  background: var(--bg-hover);
  color: var(--text-1);
}
.nav-item.active {
  background: var(--bg-active);
  color: var(--text-1);
}
.nav-icon {
  width: 16px;
  text-align: center;
  opacity: 0.8;
}
.sidebar-foot {
  border-top: 1px solid var(--border);
  padding: 12px 12px 14px;
}
.who {
  display: flex;
  align-items: center;
  gap: 9px;
  margin-bottom: 10px;
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--bg-active);
  border: 1px solid var(--border-strong);
  display: grid;
  place-items: center;
  font-size: 13px;
}
.who-text {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  min-width: 0;
}
.who-text span {
  font-size: 12px;
}
.logout {
  width: 100%;
  justify-content: center;
}
.content {
  flex: 1;
  min-width: 0;
  overflow: auto;
  padding: 22px 26px 40px;
}
</style>
