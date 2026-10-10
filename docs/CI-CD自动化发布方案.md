# CI/CD 自动化发布方案（rag-qa-system）

> 写作日期：2026-10-10
> 目标：本地 `git push origin main` → 服务器自动拉代码 → 重建镜像 → 零人工介入上线。
> 适用服务器：腾讯云轻量 4C4G，IP `42.192.111.82`，用户 `ubuntu`，Docker 已装。

---

## 0. 结论先行

采用 **GitHub Actions + 自托管运行器（self-hosted runner）**，构建留在服务器本地执行。

```
本机 git push origin main
        │
        ▼
GitHub 触发 workflow（runs-on: self-hosted）
        │  （runner 就跑在 42.192.111.82 上）
        ▼
服务器执行 scripts/deploy.sh：
   1. git fetch + git reset --hard origin/main   # 拉最新代码
   2. docker compose up -d mysql redis chroma    # 中间件确保健康
   3. alembic upgrade head                        # 数据库迁移
   4. docker compose --profile full up -d --build # 重建并启动全栈
   5. docker image prune -f                        # 清悬虚镜像省磁盘
   6. 探活 http://127.0.0.1:8080/api/health       # 失败则打印日志并退出
```

为什么是这套而不是别的：

| 方案 | 做法 | 评价 |
|------|------|------|
| **A. 自托管 runner（推荐）** | runner 装在服务器，workflow 直接在 `/opt/qa` 跑 git+docker | 构建留本地（腾讯镜像快）、不动 `.env`/数据、无需 SSH 密钥、最贴合「服务器自动拉代码」 |
| B. 云端 runner SSH 进服务器 | GitHub 云端机 SSH 到服务器执行部署 | 也能用，但要管 SSH 密钥 secret，且服务器也得先变成 git 仓库，多一道手续 |
| C. CI 构建镜像推到仓库（GHCR）再拉 | 镜像推到容器 registry，服务器只 `pull` | 构建移到 GitHub，但 Dockerfile 依赖腾讯云 pip 镜像，海外 runner 可能拉不动 → 不推荐 |

---

## 1. 安全性与数据不丢的论证（必须先确认）

- `.env` 已被 `.gitignore` 排除，`git reset --hard` 只动**被跟踪的文件**，绝不碰 `.env`。
- `upload/`、`chroma_data/`、`.venv`、`models` 同样被 `.gitignore` 排除且是 bind mount/本地目录，`git reset --hard` 与 `docker compose up -d --build` 都不会删除它们。
- 业务数据真相源：MySQL 在 `mysql_data` volume，Redis 在 `redis_data` volume，向量在 `chroma_data` bind mount，原文件在 `upload` bind mount —— 全部是**容器外持久化**，重建容器不丢。
- 根目录 `.dockerignore` 已排除 `.env/models/upload/...`，`COPY . .` 不会把密钥/模型打进镜像层。
- 迁移用 `alembic upgrade head`，只前向升级、不删数据；每次部署幂等。

> ⚠️ 唯一会丢数据的命令是 `docker compose down -v`，本方案**绝不使用**。

---

## 2. 一次性准备（只需做一次）

### 2.1 把服务器 `/opt/qa` 变成 git 仓库（保留现有生产数据）

当前 `/opt/qa` 是 tar 解出来的，没有 `.git`。**不要删目录重建**（会丢 `upload/` 和 `chroma_data/` 里的生产知识库）。原地初始化：

```bash
ssh ubuntu@42.192.111.82
cd /opt/qa
git init
git remote add origin git@github.com:d-ouyang/rag_qa_system.git
# 生成「只读部署密钥」并加到 GitHub 仓库 Settings → Deploy keys（见 2.2）
git fetch origin main
git checkout -f origin/main     # 强制用 main 的跟踪文件覆盖本地，忽略 upload/chroma_data/.env
```

`git checkout -f` 只覆盖被跟踪文件，会保留 `upload/`、`chroma_data/`、`.env`（均未被跟踪/被忽略）。

### 2.2 给服务器配 GitHub 只读部署密钥（SSH）

```bash
# 在服务器上生成一对密钥（专给本服务器用）
ssh-keygen -t ed25519 -C "qa-server-deploy" -f ~/.ssh/qa_deploy -N ""
cat ~/.ssh/qa_deploy.pub
```

把公钥内容加到 GitHub 仓库 **Settings → Deploy keys → Add deploy key**，勾选 **Read-only**（只需拉取）。
然后在服务器 `~/.ssh/config` 加一段，让 `git@github.com` 走这把密钥：

```
Host github.com
  IdentityFile ~/.ssh/qa_deploy
  IdentitiesOnly yes
```

验证：`ssh -T git@github.com` 应能识别（返回 "You've successfully authenticated" 即可，不用能 shell）。

### 2.3 在服务器安装 GitHub Actions 自托管 runner

GitHub 仓库 **Settings → Actions → Runners → New self-hosted runner**，选 Linux x64，页面会给出带一次性 token 的精确命令，形如：

```bash
ssh ubuntu@42.192.111.82
mkdir -p /opt/actions-runner && cd /opt/actions-runner
# 下载 runner（页面给的下载链接，下面是示例，以页面为准）
curl -o actions-runner-linux-x64.tar.gz -L https://github.com/actions/runner/releases/download/v2.xx.x/actions-runner-linux-x64-2.xx.x.tar.gz
tar xzf ./actions-runner-linux-x64.tar.gz
./config.sh --url https://github.com/d-ouyang/rag_qa_system --token <页面给的TOKEN>
sudo ./svc.sh install
sudo ./svc.sh start
```

- runner 默认以执行 `config.sh` 的用户（`ubuntu`）运行；`ubuntu` 已在 `docker` 组，能直接跑 `docker` 不用 sudo。
- 装好后回到 GitHub Runners 页面，应看到一个 **Idle** 状态的 runner。

### 2.4 把本次新增的文件提交并首次手动部署（bootstrap）

本方案新增 `docs/CI-CD自动化发布方案.md`、`.github/workflows/deploy.yml`、`scripts/deploy.sh`。
先在本机提交并 push，然后在服务器手动跑一次把 `deploy.sh` 拉下来并验证链路：

```bash
# 本机
git add .github/workflows/deploy.yml scripts/deploy.sh docs/CI-CD自动化发布方案.md
git commit -m "ci: 自托管 runner 自动部署流水线"
git push origin main

# 服务器（首次，之后就全自动了）
cd /opt/qa && git fetch --all --prune && git reset --hard origin/main
bash scripts/deploy.sh
```

---

## 3. 之后日常怎么用

1. 本地改完代码，`git commit` + `git push origin main`。
2. GitHub 自动触发 `Deploy to server` workflow，runner 在服务器上拉代码、迁移、重建、探活。
3. 打开 `https://qa.nianan.site`（或自测期 `http://42.192.111.82:8080`）看结果。
4. 在 GitHub **Actions** 页可实时看每一步日志；失败会停在 `deploy.sh` 的探活步骤并打印容器日志。

---

## 4. 回滚

构建用的是 `:latest` 标签，回滚 = 把代码退回上一个 commit 再部署：

```bash
# 服务器上
cd /opt/qa
git reset --hard <上一个稳定commit>
bash scripts/deploy.sh
```

或在本机 `git revert` / 切回 tag 再 push，触发新一轮自动部署。
（后续如需更丝滑，可给镜像打版本 tag，部署时 `docker compose up -d app:某tag`，但当前 `:latest` + commit 回退已够用。）

---

## 5. 注意事项 / 坑

1. **构建耗时**：首次自动构建 5~15 分钟（拉 torch CPU 层 + 前端 vite 构建），之后依赖层走缓存，通常 3~8 分钟。workflow 已设 `timeout-minutes: 40`。
2. **并发部署**：`concurrency` 已设串行，避免两次 push 打架。单 runner 天然一次只跑一个 job。
3. **runner 安全**：自托管 runner 等于「能 push 本仓库的人能在你服务器上执行任意命令」。个人单开发者仓库可接受；若多人协作需收紧分支保护 / 审阅。
4. **迁移时机**：`alembic upgrade head` 在 `up -d --build` **之前**跑，确保新容器起来时库结构已是最新。若某次没改模型/迁移，这步幂等无副作用。
5. **`BIND_ADDR` 与 TLS 阶段切换**：部署脚本不碰 `.env`。备案后你把 `.env` 里 `BIND_ADDR` 改回 `127.0.0.1` + 上边缘 nginx，自动部署流程完全不变。
6. **前端/网关构建上下文**：根 `.dockerignore` 已排除无关目录；`frontend/`、`gateway/` 各自构建上下文如需进一步提速，可补 `frontend/.dockerignore` / `gateway/.dockerignore` 排除其 `node_modules`（非必须，npm ci 会重装）。
7. **首次 runner 安装后务必确认 `/opt/qa` 是 git 仓库且部署密钥能拉取**，否则 workflow 会卡在 `git fetch`。
