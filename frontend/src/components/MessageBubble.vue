<script setup lang="ts">
/** 单条消息气泡：用户右侧、助手左侧；助手消息可展开溯源资料 */
import { ref } from 'vue'
import type { ChatMessage } from '@/types'

defineProps<{ message: ChatMessage }>()
const showSources = ref(false)

function fileName(source: string): string {
  const parts = source.split('/')
  return parts[parts.length - 1] || source
}
</script>

<template>
  <div class="bubble-row" :class="message.role">
    <div v-if="message.role === 'assistant'" class="avatar ai">R</div>
    <div class="bubble" :class="message.role">
      <!-- 流式等待首帧：意图识别 + LLM 首包需要数秒，先给明确的占位反馈 -->
      <div v-if="message.streaming && !message.content" class="thinking">
        正在思考<span class="dots"><i /><i /><i /></span>
      </div>
      <div v-else class="content">{{ message.content }}<span v-if="message.streaming" class="cursor" /></div>

      <!-- 溯源资料：可折叠 -->
      <template v-if="message.role === 'assistant' && message.sources && message.sources.length > 0">
        <button class="sources-toggle" @click="showSources = !showSources">
          引用资料 {{ message.sources.length }} 条 {{ showSources ? '▲' : '▼' }}
        </button>
        <div v-if="showSources" class="sources">
          <div v-for="s in message.sources" :key="s.index" class="source-item">
            <div class="source-head">
              <span class="source-idx">资料{{ s.index }}</span>
              <span class="source-name" :title="s.source">{{ fileName(s.source) }}</span>
              <span v-if="s.rerank_score != null" class="source-score">重排 {{ s.rerank_score.toFixed(3) }}</span>
            </div>
            <div class="source-snippet">{{ s.snippet }}</div>
          </div>
        </div>
      </template>
    </div>
    <div v-if="message.role === 'user'" class="avatar user">我</div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.bubble-row.user {
  flex-direction: row-reverse;
}
.avatar {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
}
.avatar.ai {
  background: linear-gradient(135deg, #4f6ef7, #7b5bf2);
}
.avatar.user {
  background: #35c56e;
}
.bubble {
  max-width: min(72%, 760px);
  border-radius: 12px;
  padding: 10px 14px;
  font-size: 14px;
  line-height: 1.75;
}
.bubble.user {
  background: var(--primary);
  color: #fff;
  border-top-right-radius: 4px;
}
.bubble.assistant {
  background: var(--bg-app);
  border: 1px solid var(--border);
  border-top-left-radius: 4px;
}
.content {
  white-space: pre-wrap;
  word-break: break-word;
}
.thinking {
  color: var(--text-3);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 2px;
}
.dots {
  display: inline-flex;
  gap: 3px;
  margin-left: 3px;
}
.dots i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--text-3);
  animation: dot-bounce 1.2s ease-in-out infinite;
}
.dots i:nth-child(2) {
  animation-delay: 0.2s;
}
.dots i:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes dot-bounce {
  0%, 60%, 100% {
    opacity: 0.3;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}
.cursor {
  display: inline-block;
  width: 7px;
  height: 15px;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--primary);
  animation: blink 0.9s step-end infinite;
}
@keyframes blink {
  50% {
    opacity: 0;
  }
}
.sources-toggle {
  margin-top: 8px;
  font-size: 12px;
  color: var(--primary);
}
.sources {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.source-item {
  background: var(--bg-content);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  font-size: 12px;
}
.source-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.source-idx {
  color: var(--primary);
  font-weight: 600;
}
.source-name {
  color: var(--text-2);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-score {
  margin-left: auto;
  color: var(--text-3);
  flex-shrink: 0;
}
.source-snippet {
  color: var(--text-3);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
