# settings.py 代码审查（模块 1）

审查时间：2026-09-13　审查对象：`config/settings.py`（71 行）

## 总评

结构清晰、分组正确、注释到位。三条核心规则都落地了：
- `BASE_DIR` 用 `Path(__file__).parent.parent.resolve()`，实测指向项目根而非 `config/` ✅
- 派生路径没有放进 `.env` ✅
- `Literal` 限定枚举、类型注解齐全 ✅

实测结果：配置能加载、`.env` 覆盖生效、`data/ vector_db/ upload/` 自动创建 ✅

下面是 7 条问题，按严重程度排列。

---

## 🔴 P0 · 必须改

### 1. API Key 硬编码进源码（第 44 行）

```python
SILICONFLOW_API_KEY : str = "sk-***（真实密钥已脱敏，原值 51 字符）"
```

> ⚠️ **本行原文是真实密钥，已在首次 git 提交前脱敏。**
> 教训：文档同样被 git 跟踪，**在文档里"举例"真实密钥，性质和写进源码完全一样**——
> 这属于「为了演示问题而制造了同一个问题」。写文档时一律用占位符。

**为什么严重**：`.env` 被 `.gitignore` 排除了，但 `settings.py` **会被 git 跟踪**。
这把 key 写进代码，等于直接提交到仓库（本地仓库、远程仓库、以及任何 clone 的人都能看到）。

**违背的原则**：密钥的唯一来源应该是环境变量。代码里只能放**空字符串占位**。

**改法**：

```python
SILICONFLOW_API_KEY : str = ""      # 真实值从 .env 读
```

`.env` 里已经有了，改完照样能读到。**改完请顺手去硅基流动后台轮换一次这个 key**——它已经在我们对话里出现过两次，且刚被写进代码文件。

---

## 🟡 P1 · 建议改

### 2. 模型选型：`Qwen/Qwen3-VL-32B-Instruct` 不适合本项目

用你的 key 实测了 5 个模型的**标准 tool_calls 协议支持**（这是后面做 Agent 的硬性前提）：

| 模型 | 标准 tool_calls | 备注 |
|------|:---:|------|
| `Qwen/Qwen3-VL-32B-Instruct`（当前选的） | ❌ | 把工具调用塞进 `reasoning_content` 当纯文本输出 |
| `Qwen/Qwen2.5-7B-Instruct` | ✅ | 最便宜，够用 |
| `Qwen/Qwen3-8B` | ✅ | 新一代 8B，性价比好 |
| `Qwen/Qwen2.5-72B-Instruct` | ✅ | 质量高，价格适中 |
| `deepseek-ai/DeepSeek-V3` | ✅ | 质量高 |

VL-32B 的实际返回长这样：

```json
{"role": "assistant", "content": "", 
 "reasoning_content": "<tool_call>\n{\"name\": \"get_weather\", \"arguments\": {\"city\": \"北京\"}}\n</tool_call>"}
```

`finish_reason` 是 `stop` 而不是 `tool_calls`，标准字段是空的——**任何按 OpenAI 协议解析的框架（包括 LangChain）都拿不到这个工具调用**。

**三个问题**：
1. **VL = Vision-Language**，它的优势在图文混合输入，纯文本 RAG 用不上这个能力
2. **32B 比 8B 贵数倍、慢一截**，而你当前瓶颈根本不在模型智力（RAG 效果主要取决于检索质量）
3. **到 M4 Agent 化时会完全跑不通**，那时才发现要换，返工成本更高

**建议**：
- 开发 / 调试期：`Qwen/Qwen3-8B`（快、便宜、支持工具调用）
- 追求答案质量：`Qwen/Qwen2.5-72B-Instruct` 或 `deepseek-ai/DeepSeek-V3`
- 想完全免费离线：切回 `LLM_PROVIDER=ollama` + `qwen3.5:9b`（但本地 9B 的 tool_calls 支持也需要验证）

> 这是"优化要基于观测"的又一个例子：模型名看起来只是一个字符串，
> 但它决定了后面 Agent 能不能玩。**动手实测 5 分钟，省掉一次返工。**

---

## 🟢 P2 · 可选优化

### 3. `class Config` 改用 `SettingsConfigDict`

**`class Config` 是干什么的**：它是给 Pydantic 看的**元配置**（配置的配置）——
类里的字段是「数据」，`class Config` 里的属性是「行为设置」，告诉 Pydantic 怎么找、怎么读配置：

| 属性 | 作用 |
|------|------|
| `env_file = ".env"` | 去哪个文件读 |
| `env_file_encoding = "utf-8"` | 用什么编码读（有中文时必须，否则乱码） |
| `extra = "ignore"` | `.env` 里有代码没定义的键时，忽略而不报错 |

**为什么有黄线**：`class Config` 是 Pydantic **v1 的遗留写法**。Pylance 报的 `reportUnannotatedClassAttribute` 只是表象，真正的原因是 v2 已经不推荐它了。

**v2 正统写法**（黄线同时消失）：

```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # 字段写在 model_config 之后
```

好处：`SettingsConfigDict` 是带类型定义的字典（TypedDict），IDE 有补全和校验；而且和 Pydantic `BaseModel` 的 `model_config` 写法统一。

### 4. `ALLOWED_ORIGINS` 默认值补全（第 58 行）

```python
ALLOWED_ORIGINS : list[str] = ["http://localhost:8501"]
```

`.env` 里有完整的三项，所以实际生效是对的。但**默认值也应该写全**——万一哪天 `.env` 丢了，默认值就是最后一道防线：

```python
ALLOWED_ORIGINS : list[str] = [
    "http://localhost:5173",   # Vue 开发服务器
    "http://localhost:8501",   # Streamlit
    "http://localhost:3000",   # 备用
]
```

### 5. `.env` 里的 `VERSION` 是无效配置

字段名是 `PROJECT_VERSION`，`.env` 里写的是 `VERSION` → **永远不生效**（虽然 `extra="ignore"` 让它不报错）。

改 `.env`：
```ini
PROJECT_VERSION=2.0.0     # 原来是 VERSION=2.0.0
```
或者直接删掉这一行。

### 6. 格式细节

| 位置 | 现状 | 建议 |
|------|------|------|
| 全文 | `PROJECT_NAME:str` 与 `DATA_DIR: Path` 混用，冒号后空格不统一 | 统一成 `字段名: 类型 = 值`（PEP 8） |
| 第 31 行 | `     # LLM配置` 缩进多一个空格 | 对齐到 4 空格 |

---

## ✅ 值得肯定的地方

1. **`BASE_DIR` 写对了**——`parent.parent` 两层，实测路径正确（这是新手最常错的地方，多一层少一层都不会报错，只是路径全跑偏）
2. **派生路径没有污染 `.env`**——正确理解了"哪些该外部配、哪些该代码算"
3. **`Literal` 用对了**——写错 provider 会启动即报错
4. **目录创建用 `exist_ok=True`**——第二次启动不会崩
5. **`.env` 与代码字段对应良好**——24 个键里只有 `VERSION` 一个对不上
6. **没有把 `PROJECT_DESCRIPTION` 漏掉**——还主动加了 FastAPI 用得到的描述字段

---

## 复习：SECRET_KEY 该填什么

**它是什么**：用于「签名 / 加密」的密钥，未来会用在 JWT 签名、session 加密、CSRF token 这类场景。

**现状**：当前项目**没有任何代码读它**（我查过原项目，定义了但从未使用）。所以现在填什么都能跑。

**但要提前做对**：

```bash
# 生成一个真正的随机密钥（32 字节，URL 安全）
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

然后写进 `.env`：
```ini
SECRET_KEY=<生成的那串>
```

代码里保持 `SECRET_KEY : str = ""` 或占位默认值。

**铁律**：绝不能留 `"your-secret-key"` 这种默认值上线。原因是**攻击者会优先尝试常见默认值**——等于把门钥匙挂在门把手上。这也是为什么它必须来自环境变量，而不是代码。

---

## 修改清单（按顺序做）

- [ ] 1. `settings.py:44` 密钥改成 `""`，然后**轮换该 key**
- [ ] 2. 模型换成 `Qwen/Qwen3-8B`（同时改 `.env` 和代码默认值）
- [ ] 3. `class Config` → `SettingsConfigDict`
- [ ] 4. `ALLOWED_ORIGINS` 默认值补全
- [ ] 5. `.env` 里 `VERSION` → `PROJECT_VERSION`
- [ ] 6. 格式统一（可选）
- [ ] 7. 改完跑一次验证脚本（见下）

改完后自测：

```bash
cd ~/Desktop/AIAgent/rag-qa-system
.venv/bin/python -c "
from config.settings import settings
print('BASE_DIR      :', settings.BASE_DIR)
print('模型          :', settings.SILICONFLOW_MODEL_NAME)
print('密钥来源      :', 'env' if settings.SILICONFLOW_API_KEY else '空（错误）')
print('CORS          :', settings.ALLOWED_ORIGINS)
print('SECRET_KEY 长度:', len(settings.SECRET_KEY))
"
```
