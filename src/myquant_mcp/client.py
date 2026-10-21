"""掘金（MyQuant）GM SDK 封装。

设计要点：
1. **懒连接 + 幂等**：只在第一次真正取数时调用 set_serv_addr / set_token，
   避免 import 阶段就因终端未启动而报错。
2. **线程安全**：gm SDK 是同步阻塞且带全局状态的，用锁串行化连接动作，
   取数通过 asyncio.to_thread 放到线程池，不阻塞 MCP 事件循环。
3. **错误分层**：配置缺失 / 连不上终端 / 鉴权失败 / 无数据，分别给出
   可操作的提示，而不是把 SDK 的原始异常直接抛给上层。
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from .serialize import records_to_jsonable, trim_empty_strings

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_SERV_ADDR = "127.0.0.1:7001"

#: 掘金支持的 K 线频率（股票/期货通用子集）
#: 文档：分钟线支持 60s 及 60s 的整数倍，另有 1d；tick 为快照行情
SUPPORTED_FREQUENCIES: dict[str, str] = {
    "tick": "快照行情（实时/历史，历史仅最近约 3 个月）",
    "60s": "1 分钟 K 线",
    "300s": "5 分钟 K 线",
    "900s": "15 分钟 K 线",
    "1800s": "30 分钟 K 线",
    "3600s": "60 分钟 K 线",
    "1d": "日线",
}

#: 复权方式 -> 掘金常量值
ADJUST_MAP: dict[str, int] = {
    "none": 0,      # ADJUST_NONE  不复权
    "prev": 1,      # ADJUST_PREV  前复权
    "post": 2,      # ADJUST_POST  后复权
}

#: 掘金单次返回数据量上限（文档：单次最大 33000 条）
MAX_ROWS_PER_CALL = 33_000


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class GMError(Exception):
    """掘金 MCP 的基类异常，message 直接面向用户。"""


class GMConfigError(GMError):
    """配置缺失或不合法（例如没设置 token）。"""


class GMConnectionError(GMError):
    """连不上掘金终端，或终端未运行。"""


class GMAuthError(GMError):
    """token 无效或已过期。"""


class GMDataError(GMError):
    """参数或数据层面的问题（标的不存在、无数据、超出上限等）。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class GMConfig:
    """从环境变量读取的运行配置。

    环境变量：
    - ``GM_TOKEN``     掘金终端「系统设置 → 密钥管理」里的 token（必填）
    - ``GM_SERV_ADDR`` 终端地址，默认 ``127.0.0.1:7001``。
      当 MCP 服务和掘金终端不在同一台机器时，填终端的 ``IP:7001``。
    """

    token: str = ""
    serv_addr: str = DEFAULT_SERV_ADDR
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "GMConfig":
        return cls(
            token=os.environ.get("GM_TOKEN", "").strip(),
            serv_addr=os.environ.get("GM_SERV_ADDR", DEFAULT_SERV_ADDR).strip(),
        )

    def validate(self) -> None:
        if not self.token:
            raise GMConfigError(
                "缺少 GM_TOKEN。请到掘金终端「系统设置 → 密钥管理」复制 token，"
                "然后设置环境变量 GM_TOKEN；或写入 .env 文件。"
            )
        if ":" not in self.serv_addr:
            raise GMConfigError(
                f"GM_SERV_ADDR 格式应为 'host:port'，当前为 {self.serv_addr!r}。"
                "本地终端用 127.0.0.1:7001；远程终端用 <终端机器IP>:7001。"
            )


# ---------------------------------------------------------------------------
# 标的代码规范化
# ---------------------------------------------------------------------------

_EXCHANGE_ALIASES = {
    "SH": "SHSE", "SHSE": "SHSE", "SSE": "SHSE", "上海": "SHSE",
    "SZ": "SZSE", "SZSE": "SZSE", "深证": "SZSE", "深圳": "SZSE",
}


def normalize_symbol(raw: str) -> str:
    """把常见写法统一成掘金要求的 ``EXCHANGE.CODE`` 格式。

    支持：
    - ``SHSE.600000`` / ``shse.600000``（原样，仅统一大小写）
    - ``sh600000`` / ``SH600000``
    - ``600000.SH`` / ``600000.SHSE``
    - ``600000``（按代码首位推断交易所）
    """
    if not raw or not isinstance(raw, str):
        raise GMDataError("标的代码不能为空。")

    s = raw.strip().replace(" ", "")

    # 已经是 EXCHANGE.CODE
    if "." in s:
        left, right = s.split(".", 1)
        if re.fullmatch(r"[A-Za-z]{2,5}", left):
            exch = _EXCHANGE_ALIASES.get(left.upper())
            if exch:
                return f"{exch}.{right.upper()}"
        # 形如 600000.SH
        if re.fullmatch(r"[A-Za-z]{2,5}", right):
            exch = _EXCHANGE_ALIASES.get(right.upper())
            if exch:
                return f"{exch}.{left.upper()}"
        raise GMDataError(
            f"无法识别的标的格式：{raw!r}。请用 EXCHANGE.CODE，例如 SHSE.600000。"
        )

    # 前缀写法 sh600000 / sz000001
    m = re.fullmatch(r"([A-Za-z]{2})(\d{5,6})", s)
    if m:
        exch = _EXCHANGE_ALIASES.get(m.group(1).upper())
        if exch:
            return f"{exch}.{m.group(2)}"

    # 纯数字，按首位推断
    if re.fullmatch(r"\d{5,6}", s):
        head = s[0]
        if head in "65":      # 6=沪市股票, 5=沪市基金/ETF
            return f"SHSE.{s}"
        if head in "0123":    # 0/3=深市股票, 1=深市基金
            return f"SZSE.{s}"
        raise GMDataError(
            f"无法从代码 {s} 推断交易所（首位 {head}），请显式指定，例如 SHSE.{s}。"
        )

    raise GMDataError(
        f"无法识别的标的格式：{raw!r}。支持 SHSE.600000 / sh600000 / 600000.SH / 600000。"
    )


def validate_frequency(frequency: str) -> str:
    """校验频率是否在支持列表内。"""
    f = (frequency or "").strip().lower()
    if f not in SUPPORTED_FREQUENCIES:
        raise GMDataError(
            f"不支持的频率 {frequency!r}。可用：{', '.join(SUPPORTED_FREQUENCIES)}"
        )
    return f


def resolve_adjust(adjust: str | int | None) -> int:
    """把复权参数转成掘金常量。默认不复权。"""
    if adjust is None:
        return ADJUST_MAP["none"]
    if isinstance(adjust, int):
        if adjust not in ADJUST_MAP.values():
            raise GMDataError("adjust 只能是 0(不复权) / 1(前复权) / 2(后复权)。")
        return adjust
    key = str(adjust).strip().lower()
    if key not in ADJUST_MAP:
        raise GMDataError(
            f"adjust 取值无效：{adjust!r}。可用：none / prev / post（或 0 / 1 / 2）。"
        )
    return ADJUST_MAP[key]


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class GMClient:
    """对 gm SDK 的薄封装，负责连接管理与数据规整。"""

    _connect_lock = threading.Lock()
    _connected = False

    def __init__(self, config: GMConfig | None = None) -> None:
        self.config = config or GMConfig.from_env()
        self._gm: Any = None

    # -- 内部 ---------------------------------------------------------------

    def _load_sdk(self) -> Any:
        """延迟导入 gm SDK，缺失时给出安装指引。"""
        if self._gm is not None:
            return self._gm
        try:
            from gm import api as gm_api  # type: ignore
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise GMError(
                "未安装掘金 SDK。请先执行：pip install gm "
                "（国内可用 -i https://mirrors.aliyun.com/pypi/simple/）"
            ) from exc
        self._gm = gm_api
        return gm_api

    def connect(self) -> None:
        """建立与掘金终端的连接（幂等、线程安全）。"""
        self.config.validate()
        gm_api = self._load_sdk()

        with self._connect_lock:
            if GMClient._connected:
                return
            try:
                gm_api.set_serv_addr(self.config.serv_addr)
            except Exception as exc:  # noqa: BLE001 - SDK 异常类型不稳定
                raise GMConnectionError(
                    f"设置终端地址 {self.config.serv_addr} 失败：{exc}"
                ) from exc
            try:
                gm_api.set_token(self.config.token)
            except Exception as exc:  # noqa: BLE001
                self._raise_auth_or_conn(exc)
            GMClient._connected = True

    @staticmethod
    def _raise_auth_or_conn(exc: Exception) -> None:
        """把 SDK 的鉴权/连接异常翻译成人话。

        注意：掘金 SDK 的错误信息中英混杂，既有 ``connect refused`` 也有
        ``{"status": 1001, "message": "无法连接到终端服务"}``，因此关键词
        表里必须同时覆盖中英文，否则会被当成泛化错误。
        """
        text = str(exc).lower()

        # 连接类：优先判断，因为「无法连接到终端服务」里不含鉴权词
        conn_keys = (
            "connect", "refused", "timeout", "socket", "serv", "network",
            "unreachable", "重置", "拒绝",
            "连接", "终端", "无法连接", "超时", "网络", "断开",
        )
        auth_keys = (
            "token", "auth", "permission", "401", "403", "expired", "unauthorized",
            "鉴权", "密钥", "授权", "无效的令牌", "登录",
        )

        is_auth = any(k in text for k in auth_keys)
        is_conn = any(k in text for k in conn_keys)

        if is_auth and not is_conn:
            raise GMAuthError(
                f"掘金鉴权失败：{exc}\n"
                "请检查 GM_TOKEN 是否有效。token 在掘金终端「系统设置 → 密钥管理」中，"
                "重新生成后旧 token 会立即失效。"
            ) from exc

        if is_conn:
            raise GMConnectionError(
                f"连接掘金终端失败：{exc}\n"
                "请依次确认：\n"
                "1) 掘金终端已启动并完成登录（不是只开了登录框）；\n"
                "2) GM_SERV_ADDR 指向终端所在机器的地址与端口，默认 127.0.0.1:7001；\n"
                "3) MCP 服务与终端不在同一台机器时，填 <终端IP>:7001，并放行防火墙 7001 端口；\n"
                "4) 容器/虚拟机场景下 127.0.0.1 指向自身，需改用宿主机地址"
                "（Docker Desktop 可用 host.docker.internal:7001）。"
            ) from exc

        raise GMError(f"调用掘金 SDK 失败：{exc}") from exc

    def _execute(self, func_name: str, /, **kwargs: Any) -> list[dict[str, Any]]:
        """执行一次 SDK 取数调用并规整返回值。"""
        self.connect()
        gm_api = self._load_sdk()
        func = getattr(gm_api, func_name, None)
        if func is None:  # pragma: no cover
            raise GMError(f"当前 gm SDK 版本不支持 {func_name}，请升级 gm。")

        try:
            raw = func(**kwargs)
        except GMError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._raise_auth_or_conn(exc)
            raise  # pragma: no cover - _raise_auth_or_conn 一定抛出

        rows = records_to_jsonable(raw)
        return [trim_empty_strings(r) for r in rows]

    # -- 同步接口 -----------------------------------------------------------

    def current(self, symbols: list[str], fields: str = "") -> list[dict[str, Any]]:
        """实时快照（tick）。"""
        kwargs: dict[str, Any] = {"symbols": ",".join(symbols)}
        if fields:
            kwargs["fields"] = fields
        return self._execute("current", **kwargs)

    def history(
        self,
        symbol: str,
        frequency: str,
        start_time: str,
        end_time: str,
        fields: str | None = None,
        adjust: int = 0,
        df: bool = False,
    ) -> list[dict[str, Any]]:
        """指定时间段的历史行情。"""
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "frequency": frequency,
            "start_time": start_time,
            "end_time": end_time,
            "adjust": adjust,
            "df": df,
        }
        if fields:
            kwargs["fields"] = fields
        return self._execute("history", **kwargs)

    def history_n(
        self,
        symbol: str,
        frequency: str,
        count: int,
        end_time: str | None = None,
        fields: str | None = None,
        adjust: int = 0,
        df: bool = False,
    ) -> list[dict[str, Any]]:
        """最近 N 条历史行情。"""
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "frequency": frequency,
            "count": count,
            "adjust": adjust,
            "df": df,
        }
        if end_time:
            kwargs["end_time"] = end_time
        if fields:
            kwargs["fields"] = fields
        return self._execute("history_n", **kwargs)

    # -- 异步接口（给 MCP 用） ----------------------------------------------

    async def acurrent(self, symbols: list[str], fields: str = "") -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.current, symbols, fields)

    async def ahistory(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(lambda: self.history(**kwargs))

    async def ahistory_n(self, **kwargs: Any) -> list[dict[str, Any]]:
        return await asyncio.to_thread(lambda: self.history_n(**kwargs))

    # -- 健康检查 -----------------------------------------------------------

    async def aping(self) -> dict[str, Any]:
        """探活：尝试连接并取一条快照，用来确认链路真的通。"""
        self.config.validate()
        self.connect()
        # 用一个几乎必然存在的标的做探针
        probe = "SHSE.000001"  # 上证指数
        rows = await asyncio.to_thread(self.current, [probe], "")
        return {
            "connected": True,
            "serv_addr": self.config.serv_addr,
            "probe_symbol": probe,
            "probe_rows": len(rows),
            "probe_sample": rows[0] if rows else None,
        }


def reset_connection() -> None:
    """重置连接状态（测试用）。"""
    GMClient._connected = False
