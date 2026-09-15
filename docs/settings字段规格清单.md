# settings.py 字段规格清单

> 用途：**自己手写 `config/settings.py` 时的对照表。**
> 这里不给你成品代码，只给「有哪些配置项、每个是干嘛的、怎么命名、值从哪来」。
> 字段名是**建议值**，你可以改，但改了要同步改 `.env`。
>
> 全部规则均在本机实测（Python 3.11 + pydantic-settings 2.11.10）。

---

## 一、三条前置规则（先懂规则，再填字段）

### 规则 1：一个值该不该进配置？

满足**任意一条**才写进 settings：

| 判据 | 例子 |
|------|------|
| (a) 被 2 个以上模块读 | `CHUNK_SIZE` 被加载器读、日志里也打印 |
| (b) 换个部署环境就要改 | `LLM_PROVIDER`、`EMBEDDING_DEVICE` |
| (c) 是个「魔法数字」，起了名字才读得懂 | `CHUNK_SIZE=500`、`SEARCH_TOP_K=5` |

**三条都不满足 → 不该进配置**，直接当常量或局部变量写在用的地方。

> 反例：`BASE_DIR` 看着像配置，其实是**派生值**（由文件位置算出来）。
> 把它写进 `.env` 是错的——你在 A 机器配的路径，到 B 机器就失效了。

### 规则 2：命名规则

1. **全大写 + 下划线**（配置是常量语义，大写是给读代码的人的信号）
2. **同类事物统一后缀**，一眼能看出类型：

   | 后缀 | 含义 | 例 |
   |------|------|-----|
   | `_DIR` / `_FILE` | 路径 | `UPLOAD_DIR`、`LOG_FILE` |
   | `_MODEL_NAME` | 模型标识 | `EMBEDDING_MODEL_NAME` |
   | `_API_KEY` | 密钥 | `SILICONFLOW_API_KEY` |
   | `_BASE_URL` | 服务地址 | `OLLAMA_BASE_URL` |
   | `_SIZE` / `_OVERLAP` / `_TOP_K` | 数值参数 | `CHUNK_SIZE` |

3. **`.env` 里的键必须和字段名一字不差**（大小写不敏感，但**名字**必须相同）
4. 不用自造缩写，`API` / `URL` / `LLM` 这类通用缩写可以用

### 规则 3：值从哪来（三选一）

| 类型 | 特征 | 默认值策略 | 写进 `.env` 吗 |
|------|------|-----------|--------------|
| **派生值** | 由 `BASE_DIR` 算出来 | 在类里直接算，不给外部默认 | **不要写** |
| **普适默认值** | 大多数环境都一样 | 给合理默认，可被覆盖 | 可选（要改才写） |
| **环境/密钥相关** | 每台机器不同、含敏感信息 | 给空字符串占位 | **必须写** |

---

## 二、字段清单（按依赖顺序分组）

### A 组 · 项目标识

| 建议字段 | 类型 | 用途（谁读、干嘛） | 值怎么定 |
|---------|------|------------------|---------|
| `PROJECT_NAME` | `str` | FastAPI 的 `title`、`/health` 返回、日志头部 | 给中文项目名默认值 |
| `PROJECT_VERSION` | `str` | `/health` 和 API 文档的版本号 | 默认 `"1.0.0"` |

⚠️ **陷阱**：`.env` 里写 `VERSION=` 是**无效的**——字段叫 `PROJECT_VERSION`。
原项目就踩了这个坑（`.env` 里写 `VERSION=2.0.0`，代码里默认值 `1.0.0`，实际生效的是 1.0.0）。

### B 组 · 路径（全部由 BASE_DIR 派生，**不写进 `.env`**）

| 建议字段 | 类型 | 用途 | 值怎么定 |
|---------|------|------|---------|
| `BASE_DIR` | `Path` | 项目根目录，其他所有路径的基准 | `Path(__file__).resolve().parent.parent` |
| `DATA_DIR` | `Path` | 原始数据存放 | `BASE_DIR / "data"` |
| `VECTOR_DB_DIR` | `Path` | 向量库持久化目录 | `BASE_DIR / "vector_db"` |
| `MODELS_DIR` | `Path` | 本地模型根目录 | `BASE_DIR / "models"` |
| `UPLOAD_DIR` | `Path` | 上传文件落盘目录 | `BASE_DIR / "upload"` |
| `LOG_FILE` | `Path` | 日志文件 | `BASE_DIR / "app.log"` |

**为什么用 `Path` 不用 `str`**：可以直接用 `/` 拼路径、有 `.mkdir()` / `.exists()` / `.suffix`，不用手写字符串拼接。

**`parent.parent` 为什么是两次**：`settings.py` 位于 `config/` 目录内，
第一次 `parent` 是 `config/`，第二次才是项目根。少写一次，所有路径都会跑到 `config/` 里面去。

### C 组 · 向量库与切分

| 建议字段 | 类型 | 用途（谁读） | 值怎么定 |
|---------|------|-------------|---------|
| `VECTOR_STORE_TYPE` | `Literal["chroma","faiss"]` | `vector_store.py` 决定实例化哪个实现 | 默认 `"chroma"` |
| `EMBEDDING_MODEL_NAME` | `str` | `embedding.py` 用它拼本地模型目录名 | 默认 `"bge_small_zh"`，**必须与 `models/` 下的子目录名一致** |
| `EMBEDDING_DEVICE` | `str` | `embedding.py` 传给模型，决定在哪跑 | `cpu` / `mps`（Mac GPU）/ `cuda` |
| `CHUNK_SIZE` | `int` | `document_loader.py` 每块多少字 | 默认 `500` |
| `CHUNK_OVERLAP` | `int` | 相邻块重叠多少字，防切断语义 | 默认 `50`（惯例是 size 的 10%~20%） |

> `CHUNK_SIZE` / `CHUNK_OVERLAP` 是**影响 RAG 效果最狠的两个参数**，比换嵌入模型影响大。
> 它们必须进配置，且必须能在不改代码的情况下调。

### D 组 · 检索

| 建议字段 | 类型 | 用途（谁读） | 值怎么定 |
|---------|------|-------------|---------|
| `SEARCH_TOP_K` | `int` | `retriever.py` 召回几条 | 默认 `5`。太小会漏，太大引入噪声还费 token |
| `USE_RERANKER` | `bool` | `retriever.py` 决定是否创建重排器 | 默认 `True`。`.env` 里写 `true` / `false`（小写，不是 `True`） |
| `RERANKER_MODEL_NAME` | `str` | `retriever.py` 拼重排模型路径 | 默认 `"bge_reranker_base"`，同样对应 `models/` 下的目录名 |

> 设计意图：`USE_RERANKER` 单独做成开关，是为了让你能**对比开/关的效果**。
> 这也是"优化要基于观测"的落地方式——先关掉跑一遍，看正确片段掉到第几名，再决定要不要开。

### E 组 · 大模型（三个 provider 各一套，一一对应）

| 建议字段 | 类型 | 用途（谁读） | 值怎么定 |
|---------|------|-------------|---------|
| `LLM_PROVIDER` | `Literal["ollama","openai","siliconflow"]` | `llm_client.py` 工厂分支 | 默认 `"siliconflow"` |
| `OLLAMA_BASE_URL` | `str` | 本地 Ollama 服务地址 | 默认 `"http://localhost:11434"` |
| `OLLAMA_MODEL_NAME` | `str` | 本地模型名 | 如 `"qwen3.5:9b"` |
| `OPENAI_API_KEY` | `str` | OpenAI 密钥 | 默认空串，从 `.env` 来 |
| `OPENAI_BASE_URL` | `str` | OpenAI 地址（可指向兼容服务） | 默认官方地址 |
| `OPENAI_MODEL_NAME` | `str` | OpenAI 模型名 | 如 `"gpt-4o-mini"` |
| `SILICONFLOW_API_KEY` | `str` | 硅基流动密钥 | 默认空串，从 `.env` 来 |
| `SILICONFLOW_BASE_URL` | `str` | 硅基流动地址 | 默认 `"https://api.siliconflow.cn/v1"` |
| `SILICONFLOW_MODEL_NAME` | `str` | 硅基流动模型名 | 如 `"Qwen/Qwen2.5-7B-Instruct"` |

**为什么三套并存，而不是一个通用变量**：切换 provider 时要能**保留另一套配置**，
方便来回对比（本地慢但免费 / 云端快但花钱）。用一个通用变量会把配置冲掉。

⚠️ **两个必须注意的点**：
1. 模型名的字段后缀必须是 `_MODEL_NAME`，不能简写成 `_MODEL`
2. 密钥字段**只给空字符串默认值**，真实值一律从 `.env` 读——绝不硬编码进代码

### F 组 · 服务与安全

| 建议字段 | 类型 | 用途（谁读） | 值怎么定 |
|---------|------|-------------|---------|
| `API_HOST` | `str` | `run_api.py` 绑定地址 | 默认 `"0.0.0.0"`（本机开发）或 `"localhost"` |
| `API_PORT` | `int` | 后端端口 | 默认 `8000` |
| `STREAMLIT_PORT` | `int` | Streamlit 端口 | 默认 `8501` |
| `ALLOWED_ORIGINS` | `list[str]` | `api/main.py` 的 CORS 白名单 | 前端端口都加进来 |
| `SECRET_KEY` | `str` | 预留给签名/加密用 | 暂时给占位默认值 |
| `LOG_LEVEL` | `str` | `logging_config.py` 和 uvicorn 的日志级别 | 默认 `"INFO"` |

⚠️ **`ALLOWED_ORIGINS` 的 `.env` 写法**（实测结论）：

```ini
# ✅ 正确：JSON 数组
ALLOWED_ORIGINS=["http://localhost:5173","http://localhost:8501"]

# ❌ 错误：逗号分隔 —— 启动直接报 SettingsError
ALLOWED_ORIGINS=http://localhost:5173,http://localhost:8501
```

原因：pydantic-settings 对复杂类型（list / dict）**按 JSON 解析**，不走逗号分隔。

---

## 三、Pydantic 写法要点（这是语法，不是内容）

你不必照抄字段，但这 6 条结构规则要知道：

1. **每行格式**：`字段名: 类型 = 默认值`。
   **类型注解是必须的**——这正是它比 `os.getenv()` 强的地方：`os.getenv` 拿到的永远是字符串，
   而这里声明了 `int`，`.env` 写错类型会**启动就报错**。

2. **枚举限制用 `Literal`**：`LLM_PROVIDER: Literal["ollama","openai","siliconflow"]`。
   `.env` 里写 `LLM_PROVIDER=chatgpt` 会直接报错，而不是跑到一半才发现拼错了。

3. **需要一个配置类**告诉它去哪里读 `.env`，需要指定三项：
   - 文件名
   - 编码（`utf-8`，否则中文项目名可能乱码）
   - `extra = "ignore"`（`.env` 里有代码里没定义的键时，不要报错）

4. **文件末尾实例化一次**：`settings = Settings()`。
   这不是可选项——**模块级实例化 = 导入即校验 = 全项目的启动自检**。
   任何配置错误在 `import` 那一刻就暴露，而不是等某个接口被调用。

5. **紧跟一段目录创建逻辑**：把 B 组那几个目录都建出来，
   用 `mkdir(parents=True, exist_ok=True)`。
   `parents=True` 保证多级目录能建，`exist_ok=True` 保证第二次启动不报错。

6. **不要给派生路径配 `.env` 覆盖**（见规则 1 的反例）。

---

## 四、写完自检（4 个测试，几分钟）

| # | 测试 | 期望结果 | 在验证什么 |
|---|------|---------|-----------|
| 1 | `python -c "from config.settings import settings; print(settings)"` | 打印出全部字段和值 | `.env` 是否被正确读取 |
| 2 | 临时把 `.env` 的 `CHUNK_SIZE` 改成 `abc`，再跑一次 | **启动就抛 ValidationError** | 类型校验真的生效了；测完改回 |
| 3 | 临时改 `.env` 里某个字段的**值**，看打印结果是否跟着变 | 变了 = 字段名对齐了 | 排查静默失败 |
| 4 | 删掉 `data/` 目录，再 import 一次 | 目录被自动重建 | 目录创建逻辑生效 |

> 测试 2 和 3 是重点。**它们分别验证"类型是否受控"和"配置是否真的连上了"**。
> 特别是测试 3——`.env` 字段名写错时**不会报错**，只会静默用默认值，
> 不主动验证的话，你可能几个月后才发现自己改的配置从来没生效过。

---

## 五、自查题（能答上来说明真懂了）

1. 为什么 `BASE_DIR` 不该出现在 `.env` 里？
2. 现在想加一个「每次检索最多返回多少字符」的配置，它该叫什么、放哪一组、谁读它？
3. 把 `USE_RERANKER` 改成 `false` 之后，运行时有哪些代码路径会发生变化？
4. `.env` 里写 `OLLAMA_MODEL=qwen3.5:9b`，程序会报错吗？为什么这件事比报错更危险？
5. 为什么密钥字段要给「空字符串默认值」，而不是干脆不给默认值？

---

## 附：最小结构骨架（只有壳，字段由你填）

```python
# config/settings.py
from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置：所有可变参数集中在此"""

    # ---------- A 组 · 项目标识 ----------
    # 你的字段

    # ---------- B 组 · 路径（由 BASE_DIR 派生）----------
    # 你的字段

    # ---------- C 组 · 向量库与切分 ----------
    # 你的字段

    # ---------- D 组 · 检索 ----------
    # 你的字段

    # ---------- E 组 · 大模型 ----------
    # 你的字段

    # ---------- F 组 · 服务与安全 ----------
    # 你的字段

    class Config:
        """告诉 Pydantic 去哪里读环境变量"""
        # 文件名、编码、extra 策略


# 实例化（导入即校验）
settings = Settings()

# 确保必要目录存在
for dir_path in [...]:
    ...
```
