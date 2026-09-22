# RAG 智能问答系统 · 前端

类 WorkBuddy 布局的问答工作台：Vite + Vue 3 + TypeScript + Pinia。

## 目录结构

```
frontend/
├── index.html                 # 入口 HTML
├── vite.config.ts             # Vite 配置（dev 代理 /api → 127.0.0.1:8000）
├── src/
│   ├── main.ts                # 应用入口（注册 Pinia）
│   ├── App.vue                # 根布局：左侧边栏 + 右侧内容区
│   ├── types.ts               # 与后端响应模型一一对应的 TS 类型
│   ├── api/
│   │   ├── http.ts            # fetch 封装：错误归一化 + NDJSON 流式解析
│   │   ├── qa.ts              # 问答/会话接口（/api/v1/qa/*）
│   │   ├── documents.ts       # 知识库文档接口（/api/v1/documents/*）
│   │   └── system.ts          # 系统配置接口（/api/v1/system/*）
│   ├── stores/
│   │   ├── ui.ts              # 当前激活视图 + 全局轻提示（左右两侧解耦的总线）
│   │   ├── sessions.ts        # 会话列表/当前会话/消息流/流式问答
│   │   ├── documents.ts       # 知识库文档列表/统计/上传/删除
│   │   └── settings.ts        # 系统配置
│   ├── components/
│   │   ├── AppSidebar.vue     # 左侧边栏：项目信息 + 会话列表 + 功能入口
│   │   ├── MessageBubble.vue  # 消息气泡（含可折叠溯源资料）
│   │   ├── ChatInput.vue      # 底部输入框（Enter 发送）
│   │   └── ToastStack.vue     # 顶部轻提示
│   └── views/
│       ├── ChatView.vue       # 会话主页面（右侧 chat 视图）
│       ├── KnowledgeView.vue  # 知识库文档管理（右侧 knowledge 视图）
│       └── SettingsView.vue   # 系统设置（右侧 settings 视图）
└── .env.development           # VITE_API_BASE（留空 = 走 dev 代理）
```

## 布局与交互职责划分

- **左侧边栏（AppSidebar）— 导航职责**
  - 项目信息卡：名称 / 版本 / 服务健康状态（30s 轮询 `/api/v1/system/health`）
  - 会话列表：新建、点击切换（右侧加载该会话历史 `GET /api/v1/qa/sessions/{id}`）、悬停删除
  - 底部入口：「文件传输 · 知识库」→ 知识库管理视图；「系统设置」→ 配置视图
- **右侧内容区（App.vue 按 `uiStore.activeView` 切换）— 展示职责**
  - `ChatView`：消息流（流式逐字输出 + 引用资料折叠）+ 输入框，头部展示意图/路由/耗时
  - `KnowledgeView`：统计卡片 + 拖拽上传 + 文档列表（删除即清向量片段）
  - `SettingsView`：分组展示 LLM / 检索重排 / 切分 / 记忆 / 向量库 / 意图识别等关键配置（只读）
- 两侧**零组件依赖**，仅通过 Pinia store 通信。

## 开发

```bash
npm install
npm run dev        # http://localhost:5173（/api 代理到 127.0.0.1:8000）
npm run build      # vue-tsc 类型检查 + 产物输出 dist/
```

## 依赖的后端接口

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/qa/ask/stream` | 流式问答（NDJSON：session → meta → chunk×N → done） |
| `GET /api/v1/qa/sessions` | 会话列表 |
| `GET /api/v1/qa/sessions/{id}` | 会话历史 |
| `DELETE /api/v1/qa/sessions/{id}` | 删除会话记忆 |
| `POST /api/v1/documents/upload` | 上传文档入库（解析→切分→向量化） |
| `GET /api/v1/documents/` | 知识库文档列表（按来源分组） |
| `GET /api/v1/documents/stats` | 向量库统计 |
| `DELETE /api/v1/documents/?source=` | 按来源删除文档 |
| `GET /api/v1/system/settings` | 系统关键配置（密钥脱敏） |
| `GET /api/v1/system/health` | 服务探活 |
