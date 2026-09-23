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

FROM python:3.11-slim

# PYTHONUNBUFFERED=1：让 print / logging 直接进 docker logs。
#   不开的话 Python 会缓冲 stdout，容器里看日志永远是「卡住了」，
#   而 `docker logs` 拿不到缓冲区的那部分 —— 排查时最容易误导人。
# PYTHONDONTWRITEBYTECODE=1：不写 .pyc，容器是只读使用场景，省一层无意义的文件。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：requirements 不变时走构建缓存，改业务代码不会重装一遍 torch。
# 顺序不能反（先 COPY 全部再 pip install 的话，改任何 .py 都会让这一层失效）。
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

# 锁版文件优先于 requirements.txt：后者是「能跑」的宽松版本，
# lock 是「验过」的精确版本。镜像构建要可复现，只能用 lock。
COPY . .

# ⚠️ 不要加 `--workers N`（N>1）。
# 多进程会同时改写同一个 Chroma persist_directory，导致 HNSW 索引不一致
# （查询返回 documents=None → 问答接口 500；详见 docs/iterations/v2.0.0-p0.4a 的 §3.6）。
# 后端必须是**单进程**。要横向扩容得先把向量库换成 Chroma server 模式。
EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
