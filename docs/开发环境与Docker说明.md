# 开发环境、端口与 Docker

写给还没怎么用过 Docker 的人。下面说的都是**这个项目现在的跑法**，不是 Docker 通用教程。

## 5173 和 8080 是什么关系

它们是同一套前端的两种启动方式，不是「开发服」和「测试服」，数据也不是两套。

| 地址 | 是谁 | 什么时候有 |
|------|------|------------|
| `http://127.0.0.1:5173` | Vite。直接读 `frontend/src`，改代码马上热更新 | `make frontend`（或 `make dev`） |
| `http://127.0.0.1:8080` | nginx 托管已经 `npm run build` 打好的静态文件 | `make stack-up` 把前端也放进容器之后 |

浏览器打开 5173 时，页面里的 `/api` 请求由 Vite 转到本机网关 `3000`，网关再转到后端 `8000`。

8080 是「按上线方式跑一遍」：前端不再用 Vite，容器里的 nginx 托管构建产物，并反代 `/api`。要看到最新前端改动，要么开着 5173，要么重新构建前端镜像后再看 8080。

## 现在这台机器上各端口是谁

日常开发是「中间件在 Docker，应用在本机」：

| 地址 | 服务 | 跑在哪 |
|------|------|--------|
| `127.0.0.1:5173` | 前端 Vite | 本机 node |
| `*:3000` | 鉴权网关 | 本机 node |
| `127.0.0.1:8000` | 问答后端 | 本机 Python |
| `127.0.0.1:3306` | MySQL | Docker |
| `127.0.0.1:6379` | Redis | Docker |
| `127.0.0.1:8001` | Chroma server | Docker（容器内部仍是 8000，映射出来避开本机后端） |

`make stack-up` 会再把后端、解析进程、网关、前端放进容器。那时入口改成 8080，本机不要再占 8000 和 3000。

## Docker 在这个项目里管什么

镜像是安装包，容器是正在运行的那一份。第一次会下载或构建，之后启动只是把已有镜像跑起来，不会每次重新下载。改了 Dockerfile、或改了要打进镜像的代码，才需要重新构建应用镜像。MySQL、Redis、Chroma 的官方镜像拉过一次就够了。

| 东西 | 算什么 |
|------|--------|
| `frontend/src`、`api/`、`core/` | 硬盘上的普通源码。走 5173 时直接读它们，不进镜像 |
| MySQL、Redis、Chroma | 三个容器，用的是官方镜像 |
| nginx | 只在全栈模式里，作为前端容器的运行环境 |
| `rag-qa-backend` 镜像 | 只有 `make stack-up` 才构建，里面是 Python 依赖，跑后端和解析进程 |
| 数据 | MySQL、Redis 在 Docker 管理的数据卷里；Chroma 在项目目录 `chroma_data/`；上传文件在 `upload/`。停容器数据还在 |

常用命令：

```bash
make infra        # 起 mysql + redis + chroma
make infra-check  # 看这三个是否健康
make api          # 本机后端
make worker       # 本机解析
make gateway      # 本机网关
make frontend     # http://127.0.0.1:5173
make stack-up     # 六个服务全进容器，入口 http://127.0.0.1:8080
```

## Chroma 和 Chroma server

同一套向量库，两种打开方式。

**嵌入式**：进程自己打开一个目录，索引加载在这个进程的内存里。后端和解析进程各持一份时，解析进程写完磁盘，后端内存里还是旧索引，所以以前要重启后端才能搜到新文档。

**server**：单独一个容器守着 `chroma_data/`，对外提供 HTTP。后端和解析进程都来问它，写入立刻两边可见。

业务代码的检索、写入方法不用改。差别只在连接：

```python
# 嵌入式
chromadb.PersistentClient(path="vector_db")

# server（本机映射端口是 8001）
chromadb.HttpClient(host="127.0.0.1", port=8001)
```

`.env` 里 `CHROMA_HOST` 留空走嵌入式，写成 `127.0.0.1` 就走 server。当前项目用的是 server。
