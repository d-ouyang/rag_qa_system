<script setup lang="ts">
/**
 * P2-21：通用二次确认弹窗（配合 `stores/confirm.ts`）。
 * 在 AdminShell 挂载**一次**，全管理端可用 `useConfirmStore().ask(...)`。
 */
import { useConfirmStore } from '@/stores/confirm'

const confirm = useConfirmStore()
</script>

<template>
  <Teleport to="body">
    <div v-if="confirm.state" class="cf-mask" @click.self="confirm.settle(false)">
      <div class="cf-panel" role="alertdialog" aria-modal="true">
        <h3 class="cf-title">{{ confirm.state.title }}</h3>
        <p v-if="confirm.state.text" class="cf-text">{{ confirm.state.text }}</p>
        <div class="cf-actions">
          <button class="btn btn-sm" @click="confirm.settle(false)">取消</button>
          <button
            class="btn btn-sm"
            :class="confirm.state.danger ? 'btn-danger' : 'btn-primary'"
            @click="confirm.settle(true)"
          >
            {{ confirm.state.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cf-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 90;
}

.cf-panel {
  width: 400px;
  max-width: calc(100vw - 40px);
  background: var(--bg-panel);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  padding: 20px;
  box-shadow: var(--shadow-pop);
}

.cf-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-1);
}

.cf-text {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--text-2);
  line-height: 1.7;
  white-space: pre-wrap; /* 调用方文案里有换行，保留 */
}

.cf-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
