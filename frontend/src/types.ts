/** 与后端 api/routes/qa.py 的响应模型一一对应 */

export type Role = 'user' | 'assistant'

/** 单条溯源信息（检索命中的资料片段） */
export interface SourceItem {
  index: number
  /**
   * 切片引用键，格式 `<doc_id>:<chunk_index>`（P0-4a 起）。
   *
   * `null` 表示这条引用**无法反查**（P0-3 之前的遗留切片，连文档编号都没有）——
   * 前端必须把它渲染成不可点击的纯文本，**不要**退化成用 `source` 路径去查：
   * 那会让用户以为「有键可查」，点下去必然失败。
   */
  chunk_id?: string | null
  source: string
  /** 原始文件名。没有时前端退回 source 的最后一段（多半是 uuid 落盘名） */
  file_name?: string | null
  snippet: string
  rerank_score?: number | null
  vector_similarity?: number | null
}

/** 切片详情（GET /api/v1/chunks/{chunk_id}，对应后端 ChunkDetail） */
export interface ChunkDetail {
  chunk_id: string
  doc_id: number
  chunk_index: number
  /** 切片全文（接口不截断；snippet 是它的前缀） */
  content: string
  char_count: number
  page?: number | null
  file_name: string
  file_type?: string | null
  file_size?: number | null
  /** 上传时间，格式 `YYYY-MM-DD HH:MM:SS`（后端按本地时区拼的字符串） */
  upload_time?: string | null
  project_id?: string | null
  /**
   * 切片在库里、但 MySQL 里没有对应文档记录时为 false（脏数据）。
   * 正文照常可用，这个标记是给运维的信号：该跑重建脚本了。
   */
  document_exists: boolean
  document_status?: string | null
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

/**
 * 文档解析状态 —— 与后端 `document.status` 的四态一一对应。
 *
 * 这个状态由**后台 Worker 推进**（P0-3a 起），不是前端轮询出来的中间态：
 * 上传接口返回的是 `pending`，解析在另一个进程里跑，前端只能读、不能推。
 * 所以四态里没有「上传中」——「上传中」是 HTTP 请求还没回来，属于另一回事。
 */
export type DocStatus = 'pending' | 'parsing' | 'success' | 'fail'

/**
 * 知识库文档（`GET /api/v1/documents/`）。
 *
 * ⚠️ 数据源是 **MySQL**，不是向量库。原因：正在解析 / 解析失败的文档在向量库里
 * 根本没有切片，只看向量库会让人以为「文件没上传成功」。
 * 所以这里有 status / fail_reason 这些向量库提供不了的字段。
 */
export interface KnowledgeDoc {
  /** document 表主键。**删除、重解析、查切片、下载都用它**（不再用磁盘路径当身份） */
  doc_id: number
  project_id: string
  /** 原始文件名（展示与下载用）。磁盘名是 uuid，见 storage_path */
  file_name: string
  /** 磁盘相对路径（uuid 名）。接口返回但**不该用于拼任何地址** */
  storage_path: string
  file_size: number
  status: DocStatus
  /** 切片数。仅 status=success 时有意义；失败会被后端归零 */
  chunk_count: number
  /** 失败原因（**给人看的文案**，status=fail 时有值；其余状态为空串） */
  fail_reason: string
  /** 被解析过几次。重试会累加，用于展示「试了 3 次还是失败」 */
  attempt_count: number
  /** 上传时间。后端统一成 `YYYY-MM-DD HH:mm:ss` 字符串（不是 ISO-T，见后端 to_dict） */
  upload_time: string
  /** 本次解析开始时间，**仅 parsing 时非空**；非空且过久 = 卡住的孤儿任务 */
  parse_started_at: string | null
}

/** 文档状态计数。四个状态**恒全量出现**（没有的补 0），所以可以直接读 */
export interface DocStatusCounts {
  pending: number
  parsing: number
  success: number
  fail: number
  total: number
}

export interface DocumentListResponse {
  /** 本次返回的条数（受 limit 限制），不是库里总数 */
  total_documents: number
  /** 向量库里的切片总数（含正在解析中的文档已写入的部分） */
  total_chunks: number
  counts: DocStatusCounts
  documents: KnowledgeDoc[]
}

/**
 * 上传受理响应（`POST /api/v1/documents/upload` → **202**）。
 *
 * ⚠️ 这里**没有 `chunks_added`** —— 响应发出的那一刻解析还没开始，
 * 那个数根本不存在。前端必须改成轮询 `status`（这就是 P0-3b 的全部理由）。
 */
export interface UploadAccepted {
  doc_id: number
  file_name: string
  /** 恒为 pending（受理成功但尚未解析） */
  status: DocStatus
  file_size: number
  /** 无点后缀，如 pdf */
  file_type: string
  project_id: string
  /** 是否成功投进解析队列。**false 表示已入库但排队失败**（消息队列不可用），可用 reparse 补投 */
  queued: boolean
  /** queued=false 时后端给的处理建议 */
  detail?: string
}

/** 批量上传里单份文件的结果（`POST /api/v1/documents/upload/batch`） */
export interface BatchUploadItem {
  ok: boolean
  file_name: string
  /** ok=true 时带受理信息（同 UploadAccepted 的字段） */
  doc_id?: number
  status?: DocStatus
  file_size?: number
  file_type?: string
  queued?: boolean
  detail?: string
  /** ok=false 时带失败原因（类型不支持 / 超限 / 空文件） */
  error?: string
}

/** 批量上传的汇总响应 */
export interface BatchUploadResult {
  total: number
  accepted: number
  skipped: number
  results: BatchUploadItem[]
}

/** 手动重解析（`POST /api/v1/documents/{doc_id}/reparse` → 202） */
export interface ReparseAccepted {
  doc_id: number
  status: DocStatus
  queued: boolean
  detail?: string
}

/** 删除文档（`DELETE /api/v1/documents/{doc_id}`）：三件事的执行结果 */
export interface DeleteDocumentResponse {
  doc_id: number
  /** 从向量库删掉的切片数 */
  deleted_chunks: number
  /** 磁盘原文件是否删掉了 */
  file_removed: boolean
  /** MySQL 记录是否删掉了 */
  record_removed: boolean
  file_name?: string
  /** 记录本来就不存在时的说明（删除是幂等的，不算错误） */
  detail?: string
}

/** 文档切分片段（`GET /api/v1/documents/{doc_id}/chunks`，按 chunk_index 升序） */
export interface DocumentChunk {
  /** 展示序号，**从 1 开始**（后端 enumerate(start=1)） */
  index: number
  content: string
  char_count: number
  /** PDF/PPT 的页码，可能没有 */
  page?: number | null
  /** 库内序号，**从 0 开始**；老数据可能没有（null） */
  chunk_index?: number | null
}

export interface DocumentChunksResponse {
  doc_id: number
  file_name: string
  status: DocStatus
  chunk_count: number
  chunks: DocumentChunk[]
}

/**
 * 解析链路运行状态（`GET /api/v1/system/queue`）。
 *
 * ⚠️ **本接口服务端会阻塞约 1 秒**：worker 存活探测走 Celery 的 inspect().ping()，
 * 它必须等满一个超时窗口才能确定「不会再有 worker 应答」。
 * 所以前端**不能**把它挂到跟文档列表同频的轮询上（见 stores/documents.ts 的降频逻辑）。
 */
export interface ParseQueueStatus {
  queue: {
    name: string
    broker_ok: boolean
    /** 待消费消息数（LLEN）；broker 不可达时为 null */
    depth: number | null
    /** 已投递未被确认的消息数 */
    unacked: number | null
    detail: string
  }
  worker: {
    ok: boolean
    /** 应答的 worker 名单，如 ["celery@host"] */
    workers: string[]
    detail: string
  }
  /** 文档状态计数（MySQL）；读失败时只有 error 字段 */
  documents: Partial<DocStatusCounts> & { error?: string }
  /** 上面那个 1 秒超时的具体数值，让调用方知道本接口最坏多慢 */
  ping_timeout_seconds: number
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
