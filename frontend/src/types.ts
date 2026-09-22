/** 与后端 api/routes/qa.py 的响应模型一一对应 */

export type Role = 'user' | 'assistant'

/** 单条溯源信息（检索命中的资料片段） */
export interface SourceItem {
  index: number
  source: string
  snippet: string
  rerank_score?: number | null
  vector_similarity?: number | null
}

/** 会话列表项（GET /api/v1/qa/sessions） */
export interface SessionInfo {
  session_id: string
  message_count: number
  last_active?: number | null
  /** 会话累计 token 用量 */
  usage?: SessionUsage
  /** 是否置顶 */
  pinned?: boolean
  /** 用户自定义标题（覆盖自动标题） */
  title?: string | null
}

export interface SessionUsage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  requests: number
}

/** 会话内消息（GET /api/v1/qa/sessions/{id} 与前端本地流式拼接共用） */
export interface ChatMessage {
  role: Role
  content: string
  /** 提问时间（Unix 秒；assistant 消息与同轮 user 消息相同） */
  ts?: number
  /** assistant 消息携带的溯源资料 */
  sources?: SourceItem[]
  /** assistant 消息的意图识别结果（流式 meta 帧返回） */
  intent?: string
  /** 本轮 token 用量与耗时（done 帧返回，历史接口按轮回填） */
  usage?: TurnUsage
  elapsed_ms?: number
  /** 是否正在流式输出中 */
  streaming?: boolean
}

/** 流式问答 NDJSON 各帧的联合类型 */
export type StreamFrame =
  | { type: 'session'; session_id: string }
  | { type: 'meta'; intent: string; route: string; intent_source: string; standalone_question?: string | null; sources?: SourceItem[] }
  | { type: 'chunk'; content: string }
  | { type: 'done'; elapsed_ms?: number; usage?: TurnUsage; session_usage?: SessionUsage }
  | { type: 'error'; status?: number; detail: string }

/** 单次问答的 token 用量（done 帧携带） */
export interface TurnUsage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
}

/** 知识库文档（GET /api/v1/documents） */
export interface KnowledgeDoc {
  source: string
  file_name: string
  file_type: string
  chunk_count: number
}

/** 文档切分片段（GET /api/v1/documents/chunks） */
export interface DocumentChunk {
  index: number
  content: string
  char_count: number
  page?: number | null
}

export interface DocumentChunksResponse {
  source: string
  chunk_count: number
  chunks: DocumentChunk[]
}

export interface DocumentListResponse {
  total_documents: number
  total_chunks: number
  documents: KnowledgeDoc[]
}

export interface VectorStats {
  vector_store_type: string
  persist_directory: string
  collection_name: string | null
  total_vectors: number
  source_count: number
  embedding_model: string
  embedding_loaded_from: string
  embedding_device: string
  embedding_dimension: number
}

/** 系统配置（GET /api/v1/system/settings），分组与后端一致 */
export interface SystemSettings {
  project: { name: string; version: string; api_host: string; api_port: number; log_level: string }
  llm: {
    provider: string
    model: string
    base_url: string
    api_key_set: boolean
    temperature: number
    max_tokens: number
    timeout_seconds: number
    max_retries: number
    reasoning_enabled: boolean | null
  }
  retrieval: {
    search_top_k: number
    use_reranker: boolean
    reranker_model: string | null
    rerank_candidate_multiplier: number
    rerank_score_threshold: number | null
  }
  chunking: { chunk_size: number; chunk_overlap: number }
  memory: { max_turns: number; session_ttl_seconds: number }
  intent: { provider: string; model: string; timeout_seconds: number }
  embedding: { model: string; device: string; dimension?: number; loaded_from?: string }
  vector_store: {
    type: string
    persist_directory: string
    collection_name?: string | null
    total_vectors?: number
    source_count?: number
  }
}
