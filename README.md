# 企业级 RAG 智能问答系统

面向企业内部文档的问答系统。上传资料后，提问会先检索知识库，再结合检索结果生成回答，并给出可点开的引用。

这个项目从 0 到 1 做成。`1.0.0`（2026-09-22）是本机能完成「上传、检索、多轮问答」的一版。`2.0.0` 在这之上继续做：登录、问答记录落库、文档异步解析、引用可反查、容器部署、模型走远程接口。大版本还在进行，当前应用版本是 `2.0.0-p1.6c`。

每一步改了什么，打开版本日志即可浏览：

**[版本日志](docs/releases.html)**

本地也可以直接用浏览器打开这一页：

```bash
make releases
```

## 系统里有什么

| 部分 | 作用 |
|------|------|
| 前端 | 登录、会话、流式回答、知识库管理。开发时由 Vite 提供页面 |
| 网关 | 登录校验、限流，并把 `/api` 转到后端 |
| 后端 | 检索、重排、生成回答、会话与文档接口 |
| 解析进程 | 上传后的文档在后台切分并写入知识库 |
| MySQL | 会话、消息、文档记录 |
| Redis | 解析队列，以及相同问题的回答缓存 |
| Chroma | 向量检索 |

对话、意图、嵌入、重排都走硅基流动，本机不加载模型文件。`.env` 里需要填写 `SILICONFLOW_API_KEY`。

## 第一次准备

需要本机已有 Python 3.11、[uv](https://docs.astral.sh/uv/)、Node.js、Docker。

```bash
cp .env.example .env
# 在 .env 里填上 SILICONFLOW_API_KEY

make setup
cd frontend && npm install
cd ../gateway && npm install
```

## 本地启动

日常开发是「数据库在容器里，应用在本机」。先起中间件，再起四个应用进程。四个应用建议分四个终端，日志才看得清。

```bash
make infra          # MySQL、Redis、Chroma
make infra-check    # 自检，全部通过再继续

make api            # 后端  http://127.0.0.1:8000
make worker         # 解析进程。不起的话，上传的文档会一直停在排队
make gateway        # 网关  http://127.0.0.1:3000
make frontend       # 页面  http://127.0.0.1:5173
```

后端、网关、前端也可以一条命令一起起（解析进程仍要单独开）：

```bash
make dev            # 8000 + 3000 + 5173，Ctrl-C 一起停
```

浏览器打开 [http://127.0.0.1:5173](http://127.0.0.1:5173)。开发默认账号 `admin` / `admin123`。页面上的 `/api` 由 Vite 转到网关，网关再转到后端。

| 地址 | 是谁 |
|------|------|
| `127.0.0.1:5173` | 前端 |
| `127.0.0.1:3000` | 网关 |
| `127.0.0.1:8000` | 后端 |
| `127.0.0.1:3306` | MySQL |
| `127.0.0.1:6379` | Redis |
| `127.0.0.1:8001` | Chroma（容器内部仍是 8000，映射出来避开后端） |

停应用：`make stop`（只释放 8000、3000、5173）。解析进程在它自己的终端里 Ctrl-C。停中间件但保留数据：`make infra-stop`。

## 用 Docker 部署

全栈模式把后端、解析进程、网关、前端也放进容器。入口不再是 5173，而是 nginx 托管的页面。

起全栈之前，先停掉本机上的后端和网关，避免 8000、3000 被占用。前端容器用的是 8080，和 5173 不冲突。

```bash
make stack-up      # 构建并启动。第一次会拉镜像、构建，比较慢
make stack-ps      # 看六个容器是否在跑
make stack-logs    # 看日志
```

浏览器打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)。账号仍是 `admin` / `admin123`（未在网关环境里改过用户表时）。

```bash
make stack-down                 # 只停四个应用容器，MySQL / Redis / Chroma 继续跑
make infra-stop                 # 连中间件一起停，数据还在
make stack-rebuild s=backend   # 改了镜像或依赖后，重建其中一个服务
```

两种跑法不要同时占同一组端口。看 5173 上的最新页面，用本地启动；看和上线相同的 nginx 路径，用 `make stack-up`。改了前端源码后，全栈模式需要重新构建前端镜像，8080 才会变。

容器里的后端不对外映射 8000，只给网关访问。Chroma 的数据在项目目录 `chroma_data/`，上传文件在 `upload/`。MySQL 和 Redis 的数据在 Docker 数据卷里，停容器不会丢。

## 目录

```
rag-qa-system/
├── api/          问答、文档、系统接口
├── core/         检索、生成、会话、缓存
├── worker/       文档解析进程
├── gateway/      登录与反向代理
├── frontend/     Vue 3 页面
├── config/       配置
├── alembic/      数据库迁移
├── docs/         版本日志与迭代记录
│   └── releases.html
├── docker-compose.yml
└── Makefile
```
