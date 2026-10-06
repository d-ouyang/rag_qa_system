<script setup lang="ts">
/**
 * 概览 —— 管理端的落地页。
 *
 * 13a 阶段这里只有一句话和一个占位卡。它存在的目的不是好看，而是让
 * 「起得来 → 登录 → 进得去」这条链路有终点：一个空白页也能验证路由、
 * 守卫、身份丢过了网关注入这几件事是否真的通了。
 */
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
</script>

<template>
  <div>
    <h1 class="page-title">概览</h1>
    <p class="page-sub">
      你好，{{ auth.profile?.display_name }}。这里是管理端 —— 员工、组织与密码管理。
    </p>

    <div class="card empty">看板在 P2-13c 落地（密码到期 / 待改密 / 长期未登录）。</div>
  </div>
</template>
