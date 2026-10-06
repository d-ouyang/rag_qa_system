<script setup lang="ts">
/**
 * 根组件：只做一件事 —— 把 Pinia / Router 的作用域接上，并在启动（或刷新）时
 * 恢复登录态。
 *
 * 为什么要在这里 await verify() 而不是全交给路由守卫：守卫只对**路由跳转**
 * 生效；应用第一次打开时没有跳转动作，若不在这里恢复，
 * 直接访问 `/users` 会先渲染一个空的 shell 再跳登录页（肉眼可见的一帧）。
 */
import { onMounted } from 'vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

onMounted(async () => {
  if (auth.isAuthenticated && auth.profile === null) {
    await auth.verify()
  }
})
</script>

<template>
  <RouterView />
</template>
