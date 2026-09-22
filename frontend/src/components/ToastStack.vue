<script setup lang="ts">
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
</script>

<template>
  <div class="toast-stack">
    <div
      v-for="t in ui.toasts"
      :key="t.id"
      class="toast"
      :class="t.kind"
    >
      {{ t.text }}
    </div>
  </div>
</template>

<style scoped>
.toast-stack {
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 1000;
  pointer-events: none;
}
.toast {
  padding: 9px 18px;
  border-radius: 8px;
  font-size: 13px;
  color: #fff;
  box-shadow: 0 4px 14px rgba(31, 35, 41, 0.16);
  animation: slide-in 0.2s ease-out;
}
.toast.success {
  background: var(--success);
}
.toast.error {
  background: var(--danger);
}
.toast.info {
  background: #3b4250;
}
@keyframes slide-in {
  from {
    opacity: 0;
    transform: translateY(-8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
</style>
