# 后端 + Worker **共用**的镜像（P1-5）。
#
# 为什么一个镜像服务两个进程（而不是给 Worker 单独写一个）：
#   解析（Worker）与问答（后端）必须跑在**完全同一套依赖**上。分成两个镜像，
#   两边各自的 requirements 会独立漂移，漂移的结果是「Worker 写进去的切片，
#   后端读出来不认识」—— 而这类问题只在跨进程时现形，单测永远测不到。
#   代价只是镜像稍大（Worker 用不到 FastAPI 那几个包），换来的是依赖必然一致。
#   Worker 只换 command，见 docker-compose.yml 的 worker 服务。
#
# 为什么是 slim 而不是 alpine：
#   alpine 用 musl libc，torch / numpy 这些包要么没有预编译 wheel（要本地编译，
#   构建动辄十几分钟），要么运行期踩到 musl 与 glibc 的行为差异。
#   slim 是 glibc，wheel 直接可用，构建快且行为与开发环境一致。
#
# ── 构建提速（2026-10-22 部署优化）──────────────────────────────────────
#   ① 基础镜像**锁定 digest**（下方 FROM），避免上游轮换 python:3.11-slim 的
#      底层镜像导致整条依赖层缓存失效（那次部署单依赖层就重装了 ~20min）。
#   ② 依赖（torch + 全部第三方包）集中到 `deps` 阶段，业务代码在 `final` 阶段
#      才 COPY —— 这就是「预烘焙基础镜像」的落地方式：deps 阶段一旦构建，
#      buildkit 会把它缓存住，下次改业务代码只重跑 COPY . .（秒级），不再碰 torch。
#   升级 Python 小版本时：先 `docker pull python:3.11-slim` 取新 digest，再改这一行。

# ⚠️ 锁 digest：当前 = python:3.11.17-slim 的 linux/amd64 镜像（服务器实测）。
FROM python:3.11-slim@sha256:cce12316475d7d56d605f76e1d811d0dc4e7227f2d4afd29951521264f504ff8 AS deps

# PYTHONUNBUFFERED=1：让 print / logging 直接进 docker logs。
#   不开的话 Python 会缓冲 stdout，容器里看日志永远是「卡住了」，
#   而 `docker logs` 拿不到缓冲区的那部分 —— 排查时最容易误导人。
# PYTHONDONTWRITEBYTECODE=1：不写 .pyc，容器是只读使用场景，省一层无意义的文件。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层（deps 阶段）：requirements 不变时走构建缓存，改业务代码不会重装一遍 torch。
# 顺序不能反（先 COPY 全部再 pip install 的话，改任何 .py 都会让这一层失效）。
COPY requirements.lock.txt ./
# ⚠️ torch 单独一层：装完即缓存。lock 层失败重试时不用重下 196MB（2026-10-09 服务器实测）。
# ⚠️ 必须先装 CPU 版 torch，再装 lock（联调实测踩到的坑，2026-09-24）：
#   requirements.lock.txt 是在 macOS 上 `uv pip compile` 的，不含 torch 在 Linux 下的
#   CUDA 传递依赖；在 Linux 容器里直接 pip install 整个 lock，pip 会为 torch 补解析出
#   整套 nvidia-* / cuda-*（几个 GB 的下载 + 镜像体积），而本项目 EMBEDDING_DEVICE=cpu
#   是纯 CPU 推理，CUDA 全是浪费（4C4G 服务器上磁盘与拉取时间都是硬成本）。
#   先装 download.pytorch.org 的 +cpu 构建：它的 metadata 不含 nvidia 依赖，
#   且按 PEP 440「2.14.0+cpu 满足 torch==2.14.0」，装 lock 时 pip 不会重装 torch、
#   也不会再补拉 CUDA 包。extra-index-url 兜住 torch 自身的普通依赖（filelock 等）。
#   镜像源用腾讯云内网（mirrors.cloud.tencent.com），国内服务器直连 pypi.org 会超时。
RUN pip install --no-cache-dir --timeout 600 --retries 10 \
      --index-url https://download.pytorch.org/whl/cpu \
      --extra-index-url https://mirrors.cloud.tencent.com/pypi/simple \
      "torch==2.14.0"

# lock 用 uv 装而不是 pip：uv 并行下载 + 内置重试，国内网络下 pip 串行拉 200+ 个包的
# 元数据动辄 40 分钟且偶发响应截断（JSONDecodeError），uv 快一个量级且抗抖动。
# torch 已按上面装好（2.14.0+cpu 满足 lock 的 torch==2.14.0），uv 不会重装、不会补拉 CUDA。
RUN pip install --no-cache-dir --timeout 600 --retries 10 uv \
 && UV_HTTP_TIMEOUT=120 uv pip install --system \
      --index-url https://mirrors.cloud.tencent.com/pypi/simple \
      --extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple \
      -r requirements.lock.txt

# ── deps 阶段到此为止：上面这些层只要 requirements.lock.txt 不变就永远缓存 ──

# final 阶段：只把业务代码 COPY 进来，复用 deps 阶段装好的全部依赖。
# 改业务代码 → 只有下面这一行重跑，上面的 torch/依赖层全部命中缓存。
FROM deps AS final

# 锁版文件优先于 requirements.txt：后者是「能跑」的宽松版本，
# lock 是「验过」的精确版本。镜像构建要可复现，只能用 lock。
COPY . .

# ⚠️ 不要加 `--workers N`（N>1）。
# 多进程会同时改写同一个 Chroma persist_directory，导致 HNSW 索引不一致
# （查询返回 documents=None → 问答接口 500；详见 docs/iterations/v2.0.0-p0.4a 的 §3.6）。
# 后端必须是**单进程**。要横向扩容得先把向量库换成 Chroma server 模式。
EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
