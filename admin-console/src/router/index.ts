/**
 * 管理端路由。
 *
 * 两个守卫，缺一不可：
 *
 *   ① 未登录 → 登录页
 *   ② 已登录但 /me 还没就绪 → 先 await verify() 再放行
 *
 * ② 常被写成「刷新页面时先 render 登录页闪一下再跳回」，观感像页面坏了。
 *   这里把它放进 beforeEach 里 await：路由在身份确认之前**根本不切**，
 *   于是没有中间帧。代价是首屏多一次请求，换来的是没有闪烁。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import LoginView from '@/views/LoginView.vue'
import AdminShell from '@/components/AdminShell.vue'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
  },
  {
    path: '/',
    component: AdminShell,
    redirect: '/overview',
    children: [
      {
        path: 'overview',
        name: 'overview',
        component: () => import('@/views/OverviewView.vue'),
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('@/views/UsersView.vue'),
      },
      {
        path: 'org',
        name: 'org',
        component: () => import('@/views/OrgView.vue'),
      },
      {
        path: 'passwords',
        name: 'passwords',
        component: () => import('@/views/PasswordsView.vue'),
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('@/views/AuditView.vue'),
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次进入（或刷新后）还没有 profile：先确认身份，别急着判未登录
  if (auth.isAuthenticated && auth.profile === null && auth.status !== 'forbidden') {
    await auth.verify()
  }
  if (!auth.isAuthenticated) {
    // 被管理端拒过的账号已经在 store 里清了凭据，回到登录页由它展示原因。
    // ⚠️ `redirect` 不能省：直接访问 /users 被拦到登录页的人，登录后理应回到
    // /users，而不是落到总览再自己点一遍（少带这个参数 = 每次深链接都要走两遍）。
    return to.path === '/login' ? true : { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login') {
    return { name: 'overview' }
  }
  return true
})

export default router
