"""
大模型客户端模块 —— 按配置生成不同厂商的 LLM 客户端（单例、可切换、不泄露密钥）。

对外提供两类东西：
    1. LLMClient：一个 provider + 一个模型的连接封装，负责构建底层 LangChain Chat 模型、
       并提供 invoke / stream 这类面向业务的调用方式。
    2. get_llm_client() / get_llm()：带缓存的获取入口（同配置复用同一实例）。

几个关键设计（面试时值得展开）：
    A. 为什么要单例
       Chat 模型对象本身不重，但它持有 HTTP 连接池与客户端配置；每次请求都新建，会造成
       连接池反复建立/销毁，且首包延迟翻倍。所以按「provider + model + 参数」缓存复用。
    B. 为什么要按 (provider, model) 缓存，而不是全局唯一
       如果只存一个全局对象，切换 provider 时就得销毁重建；
       多线程下还会出现「A 线程刚拿到 openai 连接、B 线程切成了 ollama」的串台。
       按配置维度缓存，既复用连接，又让不同配置互不干扰。
    C. 为什么要在「构造时」校验配置
       缺 API Key 如果等到第一次 invoke() 才发现，错误来自远端 401，
       排查时要区分「key 错了」「网络问题」「模型名错了」三种可能；
       前置校验能在程序启动阶段就用一条中文报错说清楚。
    D. 为什么延迟构建真正的客户端
       构造 ChatOpenAI 这类对象不会发网络请求，但 ollama 分支需要 import 可选依赖；
       延迟构建可以让「装了哪个 provider 的依赖就用哪个」，不影响其他分支。
    E. 为什么 get_model_info() 刻意不含 api_key
       这个方法会被日志、接口、前端状态页打印。一旦把 key 放进去，
       就等于把密钥写进了日志文件 —— 这是最常见的一类泄露方式。
       另外 LangChain 内部把 api_key 包成了 SecretStr，直接 str() 它会自动脱敏。
"""

import logging
import threading
import time
from collections.abc import Iterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from config.settings import settings

logger = logging.getLogger(__name__)


class LLMConfigError(ValueError):
    """
    配置错误（provider 不支持、API Key 缺失、模型名为空等）。

    单独定义异常类型的好处：上层可以只捕获「配置问题」并给出友好提示，
    而不用去 catch 一个宽泛的 ValueError。
    """


class LLMClient:
    """
    单个大模型的连接封装（一个 provider + 一个模型 = 一个实例）。

    用法：
        客户端式：get_llm_client().invoke("你好")
        供 LCEL 链：get_llm()  直接拿底层 BaseChatModel
    """

    # 支持的厂商。新增厂商时要同步改：SUPPORTED_PROVIDERS / _PROVIDER_SETTINGS / _build_llm()
    SUPPORTED_PROVIDERS: tuple[str, ...] = ("openai", "siliconflow", "ollama")

    # 需要校验 API Key 的厂商（ollama 是本地服务，不需要 key）
    KEY_REQUIRED_PROVIDERS: tuple[str, ...] = ("openai", "siliconflow")

    # provider -> (模型名设置项, base_url 设置项, api_key 设置项, 日志里的友好名称)
    # 用「配置项名字」而不是具体值，是为了让所有配置仍然集中在 settings.py 一处。
    _PROVIDER_SETTINGS: dict[str, tuple[str, str, str | None, str]] = {
        "openai": ("OPENAI_MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OpenAI"),
        "siliconflow": (
            "SILICONFLOW_MODEL_NAME",
            "SILICONFLOW_BASE_URL",
            "SILICONFLOW_API_KEY",
            "SiliconFlow",
        ),
        # ollama 是本地服务，没有 api_key 这个配置项
        "ollama": ("OLLAMA_MODEL_NAME", "OLLAMA_BASE_URL", None, "Ollama"),
    }

    def __init__(
        self,
        provider: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        """
        :param provider: 厂商名，缺省读 settings.LLM_PROVIDER
        :param model_name: 模型名，缺省读该厂商对应的默认模型
        :param api_key: 密钥，缺省读对应配置；本地 ollama 不需要
        :param base_url: 服务地址，缺省读对应配置
        :param temperature: 采样温度，缺省读 settings.LLM_TEMPERATURE
        :param max_tokens: 单次回答最大 token 数，缺省读 settings.LLM_MAX_TOKENS
        :param timeout: 单次请求超时秒数，缺省读 settings.LLM_TIMEOUT
        :param max_retries: 失败重试次数，缺省读 settings.LLM_MAX_RETRIES
        """
        # ① 归一化 provider（大小写不敏感），缺省取全局配置
        self.provider: str = (provider or settings.LLM_PROVIDER).strip().lower()

        # 白名单校验：拼错 provider 时立刻报错，而不是打到奇怪的地址上
        if self.provider not in self.SUPPORTED_PROVIDERS:
            raise LLMConfigError(
                f"不支持的 LLM provider：'{self.provider}'；"
                f"当前支持：{'/'.join(self.SUPPORTED_PROVIDERS)}"
                f"（请检查 settings.LLM_PROVIDER 或 .env 中的 LLM_PROVIDER）"
            )

        # ② 取出该厂商对应的默认配置项名；显式传入的参数优先级更高
        model_key, base_url_key, api_key_key, display_name = self._PROVIDER_SETTINGS[
            self.provider
        ]
        self.display_name: str = display_name
        self.model_name: str = model_name or str(getattr(settings, model_key))
        self.base_url: str = base_url or str(getattr(settings, base_url_key))
        # api_key 允许调用方显式覆盖（便于多租户场景）；否则从 settings 读
        self._api_key: str = (
            api_key
            if api_key is not None
            else (str(getattr(settings, api_key_key)) if api_key_key else "")
        )

        # ③ 生成参数：缺省统一读全局配置，保持「一处配置、处处生效」
        self.temperature: float = (
            temperature if temperature is not None else float(settings.LLM_TEMPERATURE)
        )
        self.max_tokens: int = (
            max_tokens if max_tokens is not None else int(settings.LLM_MAX_TOKENS)
        )
        self.timeout: int = timeout if timeout is not None else int(settings.LLM_TIMEOUT)
        self.max_retries: int = (
            max_retries if max_retries is not None else int(settings.LLM_MAX_RETRIES)
        )

        # ④ 配置校验前置：缺 key / 缺模型名在「构造时」就抛明确异常
        self._validate_config()

        # ⑤ 真正的 Chat 模型延迟到第一次使用时才构建（此处只记日志、不建连接）
        self._llm: BaseChatModel | None = None
        self._lock = threading.Lock()

        logger.info(
            "LLM 客户端已创建（延迟连接）| provider=%s model=%s base_url=%s "
            "temperature=%s max_tokens=%s timeout=%ss",
            self.display_name,
            self.model_name,
            self.base_url,
            self.temperature,
            self.max_tokens,
            self.timeout,
        )

    # ------------------------------------------------------------------ #
    # 配置校验
    # ------------------------------------------------------------------ #
    def _validate_config(self) -> None:
        """校验必要配置是否齐全，缺什么就在报错里点名什么。"""
        if self.provider in self.KEY_REQUIRED_PROVIDERS and not self._api_key.strip():
            missing_key = self._PROVIDER_SETTINGS[self.provider][2]
            raise LLMConfigError(
                f"缺少 API Key：provider='{self.provider}'，"
                f"请在 .env 中配置 {missing_key} 后重试"
            )
        if not self.model_name.strip():
            raise LLMConfigError(f"缺少模型名称：provider='{self.provider}'")

    # ------------------------------------------------------------------ #
    # 底层模型构建
    # ------------------------------------------------------------------ #
    def get_llm(self) -> BaseChatModel:
        """
        返回底层 LangChain Chat 模型（供 LCEL 链 / LangGraph 直接使用）。

        首次调用时才真正构建；用双检锁保证多线程下只构建一次。
        """
        if self._llm is None:
            with self._lock:
                if self._llm is None:      # 再次判断：可能已有线程在这一层锁内构建完成
                    self._llm = self._build_llm()
        return self._llm

    def _build_llm(self) -> BaseChatModel:
        """按 provider 构建具体的 Chat 模型实例。"""
        try:
            if self.provider == "ollama":
                built = self._build_ollama()
            else:
                # openai 与 siliconflow 都走 OpenAI 兼容协议，只是 base_url 和 key 不同
                built = self._build_openai_compatible()
        except ImportError as e:
            # 可选依赖缺失要给「可执行的修法」，不能只把原始 ImportError 抛出去
            raise ImportError(
                f"缺少 {self.display_name} 客户端依赖：{e}；"
                f"请安装后重试：uv pip install langchain-openai（ollama 用 langchain-ollama）"
            ) from e

        logger.info(
            "LLM 客户端连接就绪 | provider=%s model=%s",
            self.display_name,
            self.model_name,
        )
        return built

    def _build_openai_compatible(self) -> BaseChatModel:
        """
        构建 OpenAI 兼容客户端（OpenAI 官方 / 硅基流动共用同一套协议）。

        注意：api_key 传进去后会被 LangChain 包成 SecretStr，
        后续哪怕 print(llm) 也只会看到 **********。
        """
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=self.model_name,
            api_key=self._api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            # pyright 认为 0.2.x 的 ChatOpenAI 没有该入参（实际支持，走 model_fields 校验）
            max_tokens=self.max_tokens,  # pyright: ignore[reportCallIssue]
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def _build_ollama(self) -> BaseChatModel:
        """
        构建本地 ollama 客户端。

        优先用新的 langchain-ollama 包；没装则回退到 community 版本
        （后者已标记在 LangChain 1.0 移除，但当前版本仍可用），
        这样在不额外装依赖的情况下也能跑起来。

        与 OpenAI 兼容分支对齐的三个参数：
            · num_predict  <- self.max_tokens（ollama 侧的「最大生成 token 数」）
            · 超时：langchain-ollama 没有 timeout 字段，要塞给底层 ollama.Client
              （client_kwargs）；community 版反过来没有 client_kwargs，
              传了会被 pydantic 静默丢弃（extra="ignore"），必须用它的原生 timeout 字段。
              不传的话本地大模型的长请求会一直挂着，settings.LLM_TIMEOUT 形同虚设。
            · reasoning <- settings.OLLAMA_REASONING（关闭思考链：thinking token 不进正文，
              会让 stream 长时间吐空 chunk；仅 langchain-ollama 支持）

        两个分支的入参不同，所以各自组装 kwargs 字典再统一构造：
        既能差异传参，也顺便绕开 pyright 对单分支（community）的入参检查。
        """
        kwargs: dict[str, Any]
        try:
            from langchain_ollama import ChatOllama  # pyright: ignore[reportMissingImports]

            kwargs = {
                "model": self.model_name,
                "base_url": self.base_url,
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "client_kwargs": {"timeout": self.timeout},
                "reasoning": settings.OLLAMA_REASONING,
            }
        except ImportError:
            from langchain_community.chat_models import ChatOllama  # type: ignore[attr-defined, no-redef]

            kwargs = {
                "model": self.model_name,
                "base_url": self.base_url,
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "timeout": self.timeout,
            }

        return ChatOllama(**kwargs)

    # ------------------------------------------------------------------ #
    # 面向业务的调用
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_messages(prompt: str, system_prompt: str | None = None) -> list[BaseMessage]:
        """把「可选 system + 用户问题」组装成 LangChain 的消息列表。"""
        messages: list[BaseMessage] = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        messages.append(HumanMessage(content=prompt))
        return messages

    @staticmethod
    def _to_text(response: Any) -> str:
        """
        把模型的返回统一转成纯文本。

        不同厂商返回的 content 类型不一致：多数是 str，部分是 list[dict]
        （多模态格式形如 [{"type": "text", "text": "..."}]）。
        这里统一收敛，避免上层到处写 isinstance 判断。
        """
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ]
            return "".join(text_parts)
        return str(content)

    def invoke(self, prompt: str, system_prompt: str | None = None) -> str:
        """同步调用大模型，返回纯文本（失败会向上抛异常，由上层决定话术）。"""
        start = time.perf_counter()
        try:
            response = self.get_llm().invoke(self._build_messages(prompt, system_prompt))
        except Exception as e:
            logger.error(
                "LLM 调用失败 | provider=%s model=%s 耗时=%.2fs | 错误：%s",
                self.display_name,
                self.model_name,
                time.perf_counter() - start,
                e,
            )
            raise

        text = self._to_text(response)
        logger.info(
            "LLM 调用完成 | provider=%s model=%s 耗时=%.2fs 回复长度=%d",
            self.display_name,
            self.model_name,
            time.perf_counter() - start,
            len(text),
        )
        return text

    def stream(self, prompt: str, system_prompt: str | None = None) -> Iterator[str]:
        """
        流式调用：逐段产出文本增量。

        只 yield 非空片段 —— 部分厂商会在流里塞空 chunk，过滤掉可以让前端不用做额外处理。
        """
        logger.debug(
            "开始流式调用 | provider=%s model=%s 提示长度=%d",
            self.display_name,
            self.model_name,
            len(prompt),
        )
        for chunk in self.get_llm().stream(self._build_messages(prompt, system_prompt)):
            text = getattr(chunk, "content", "") or ""
            if text:
                yield text

    # ------------------------------------------------------------------ #
    # 状态信息
    # ------------------------------------------------------------------ #
    def get_model_info(self) -> dict[str, Any]:
        """
        返回客户端信息（供日志 / 接口 / 状态页使用）。

        ⚠️ 刻意不包含 api_key：本方法的返回值会被到处打印，
        放进密钥就等于把密钥写进日志文件。需要判断是否配置了密钥时，
        只返回布尔值 has_api_key。
        """
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "has_api_key": bool(self._api_key.strip()),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "initialized": self._llm is not None,   # 底层连接是否已真正建立
        }


# --------------------------------------------------------------------------- #
# 单例缓存
# --------------------------------------------------------------------------- #
_client_cache: dict[tuple[Any, ...], LLMClient] = {}
_cache_lock = threading.Lock()


def get_llm_client(
    provider: str | None = None,
    model_name: str | None = None,
    **kwargs: Any,
) -> LLMClient:
    """
    获取 LLM 客户端（同配置复用同一实例）。

    缓存 key 由「provider + model_name + 其余参数」组成，
    所以切 provider / 换模型都会拿到各自的实例，互不覆盖。
    """
    actual_provider = (provider or settings.LLM_PROVIDER).strip().lower()
    # 参数里有不可哈希类型时转字符串，保证能作为字典 key
    cache_key: tuple[Any, ...] = (
        actual_provider,
        model_name,
        *sorted((key, str(value)) for key, value in kwargs.items()),
    )

    client = _client_cache.get(cache_key)
    if client is None:
        with _cache_lock:
            client = _client_cache.get(cache_key)
            if client is None:                       # 双检锁
                client = LLMClient(provider=provider, model_name=model_name, **kwargs)
                _client_cache[cache_key] = client
                logger.debug("新建 LLM 客户端并缓存 | cache_key=%s", cache_key)
    return client


def get_llm(
    provider: str | None = None,
    model_name: str | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """便捷入口：直接返回底层 Chat 模型，给 LCEL 链 / LangGraph 使用。"""
    return get_llm_client(provider=provider, model_name=model_name, **kwargs).get_llm()


def reset_llm_client_cache() -> None:
    """清空客户端缓存（测试切换配置时用；生产环境通常不需要调用）。"""
    with _cache_lock:
        _client_cache.clear()
        logger.info("LLM 客户端缓存已清空")
