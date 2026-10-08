<script setup lang="ts">
/**
 * P2-21：通用二次确认弹窗（配合 `stores/confirm.ts`）。
 * 在 App.vue 挂载**一次**，全应用可用 `useConfirmStore().ask(...)`。
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
          <button class="cf-btn" @click="confirm.settle(false)">取消</button>
          <button
            class="cf-btn"
            :class="confirm.state.danger ? 'cf-btn--danger' : 'cf-btn--primary'"
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
  background: rgba(0, 0, 0, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 80;
}

.cf-panel {
  width: 380px;
  max-width: calc(100vw - 40px);
  background: var(--bg-panel, #fff);
  border: 1px solid var(--border, #e5e5e5);
  border-radius: 12px;
  padding: 20px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.24);
}

.cf-title {
  margin: 0 0 8px;
  font-size: 15px;
  font-weight: 600;
}

.cf-text {
  margin: 0 0 16px;
  font-size: 13px;
  color: var(--text-2, #666);
  line-height: 1.7;
  white-space: pre-wrap; /* 调用方的文案里有换行（\n\n），保留 */
}

.cf-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.cf-btn {
  padding: 7px 16px;
  border-radius: 8px;
  border: 1px solid var(--border, #ddd);
  background: none;
  color: inherit;
  font-size: 13px;
  cursor: pointer;
}

.cf-btn:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.05));
}

.cf-btn--primary {
  background: var(--primary, #4a6cf7);
  border-color: var(--primary, #4a6cf7);
  color: #fff;
}

.cf-btn--primary:hover {
  opacity: 0.9;
  background: var(--primary, #4a6cf7);
}

.cf-btn--danger {
  background: #d9534f;
  border-color: #d9534f;
  color: #fff;
}

.cf-btn--danger:hover {
  opacity: 0.9;
  background: #d9534f;
}
</style>
