<script setup lang="ts">
/** 底部输入区：Enter 发送 / Shift+Enter 换行，流式输出期间禁用 */
import { ref } from 'vue'

defineProps<{ disabled?: boolean; streaming?: boolean }>()
const emit = defineEmits<{ (e: 'send', question: string): void }>()

const draft = ref('')

function submit() {
  const text = draft.value.trim()
  if (!text) return
  emit('send', text)
  draft.value = ''
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  }
}
</script>

<template>
  <footer class="chat-input">
    <div class="input-box">
      <textarea
        v-model="draft"
        placeholder="输入问题，Enter 发送，Shift+Enter 换行"
        rows="1"
        :disabled="disabled"
        @keydown="onKeydown"
      />
      <button class="btn-primary send" :disabled="disabled || !draft.trim()" @click="submit">
        {{ streaming ? '生成中…' : '发送' }}
      </button>
    </div>
    <p class="input-hint">回答由大模型生成，结合知识库检索结果，仅供参考。</p>
  </footer>
</template>

<style scoped>
.chat-input {
  padding: 12px 24px 14px;
  border-top: 1px solid var(--border);
  flex-shrink: 0;
}
.input-box {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 12px;
  background: var(--bg-content);
  transition: border-color 0.15s;
}
.input-box:focus-within {
  border-color: var(--primary);
}
textarea {
  flex: 1;
  border: none;
  outline: none;
  resize: none;
  font-size: 14px;
  line-height: 1.6;
  max-height: 140px;
  font-family: inherit;
  background: transparent;
  color: var(--text-1);
}
textarea:disabled {
  cursor: not-allowed;
}
.send {
  padding: 6px 18px;
  flex-shrink: 0;
}
.input-hint {
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-3);
  text-align: center;
}
</style>
