#!/usr/bin/env python3
"""myquant-mcp — 掘金量化行情 MCP Server。

通过运行中的掘金终端（GM Terminal）提供实时行情与 K 线数据。

用法：
    python server.py                     # stdio（Claude Desktop / Cursor / Claude Code）
    MCP_TRANSPORT=streamable-http python server.py   # HTTP

环境变量：
    GM_TOKEN       掘金终端「系统设置 → 密钥管理」里的 token（必填）
    GM_SERV_ADDR   终端地址，默认 127.0.0.1:7001；跨机器时填 <终端IP>:7001
    MCP_TRANSPORT  stdio（默认）| streamable-http | sse
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# 允许 `python server.py` 直接运行（源码目录布局）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from myquant_mcp.client import (  # noqa: E402
    MAX_ROWS_PER_CALL,
    SUPPORTED_FREQUENCIES,
    GMClient,
    GMConfig,
    GMError,
    normalize_symbol,
    resolve_adjust,
    validate_frequency,
)

mcp = FastMCP("myquant-mcp")

_client: GMClient | None = None


def get_client() -> GMClient:
    """懒加载单例客户端。"""
    global _client
    if _client is None:
        _client = GMClient(GMConfig.from_env())
    return _client


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data}, ensure_ascii=False, default=str)


def _err(exc: Exception) -> str:
    payload = {
        "ok": False,
        "error": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, GMError):
        return json.dumps(payload, ensure_ascii=False)
    payload["message"] = f"未预期的错误：{exc}"
    return json.dumps(payload, ensure_ascii=False)


def _parse_symbols(raw: str) -> list[str]:
    """把用户输入的多个代码（逗号/空格/中文逗号分隔）规范化。"""
    if not raw:
        raise ValueError("symbols 不能为空。")
    parts = [p for p in raw.replace("，", ",").replace(" ", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("symbols 不能为空。")
    return [normalize_symbol(p) for p in parts]


# ---------------------------------------------------------------------------
# MCP 工具
# ---------------------------------------------------------------------------


@mcp.tool()
async def ping_gm_terminal() -> str:
    """检查与我方掘金终端的连接是否正常。

    在做任何取数之前先调用它，能立刻区分「终端没开 / 地址填错 / token 失效」
    与「数据本身取不到」这两类问题。

    Returns:
        JSON：connected、serv_addr、探针标的与样例数据。
    """
    try:
        info = await get_client().aping()
        return _ok(info)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def get_realtime_quote(symbols: str, fields: str = "") -> str:
    """获取实时行情快照（tick）。

    Args:
        symbols: 一个或多个标的，逗号分隔。支持 `600000` / `sh600000` /
            `600000.SH` / `SHSE.600000` 等写法，会自动规范化。
            例：`600000,000001`、`SHSE.600519`。
        fields: 可选，指定返回字段，逗号分隔（如 `symbol,price,open,high,low`）。
            留空返回全部字段。

    Returns:
        JSON：data 为标的快照列表，含最新价 price、开高低、成交量、
        买卖五档 quotes 等。

    Note:
        实时模式无法获取集合竞价数据（掘金限制）；实时模式下每分钟
        约每 3 秒推送一次，本工具取的是调用时刻的快照。
    """
    try:
        syms = _parse_symbols(symbols)
        rows = await get_client().acurrent(syms, fields=fields.strip())
        if not rows:
            return json.dumps(
                {"ok": True, "data": [], "hint": "未取到数据：请检查标的代码是否正确、是否在交易时段。"},
                ensure_ascii=False,
            )
        return _ok(rows)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def get_kline_history(
    symbol: str,
    frequency: str,
    start_time: str,
    end_time: str,
    adjust: str = "none",
    fields: str = "",
) -> str:
    """获取指定时间段的历史 K 线（按时间升序）。

    Args:
        symbol: 标的代码，如 `600000` 或 `SHSE.600000`。
        frequency: K 线频率。可选 `1d`（日线）、`60s`（1分）、`300s`（5分）、
            `900s`（15分）、`1800s`（30分）、`3600s`（60分）、`tick`。
        start_time: 开始时间，`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`。
        end_time: 结束时间，同上。
        adjust: 复权方式，`none`（默认，不复权）/ `prev`（前复权）/ `post`（后复权）。
            做技术分析通常用 `prev`。
        fields: 可选，指定字段，如 `open,close,high,low,volume,eob`。

    Returns:
        JSON：data 为 K 线列表，含 open/close/high/low/volume/amount/bob/eob。

    Note:
        掘金单次最多返回 33000 条，超出部分不返回。若区间过长，请分段查询。
        分钟线历史范围约从 2016 年起；tick 历史仅最近约 3 个月。
    """
    try:
        sym = normalize_symbol(symbol)
        freq = validate_frequency(frequency)
        adj = resolve_adjust(adjust)
        rows = await get_client().ahistory(
            symbol=sym,
            frequency=freq,
            start_time=start_time,
            end_time=end_time,
            fields=fields.strip() or None,
            adjust=adj,
        )
        meta = {
            "symbol": sym,
            "frequency": freq,
            "adjust": adj,
            "start_time": start_time,
            "end_time": end_time,
            "count": len(rows),
        }
        if len(rows) >= MAX_ROWS_PER_CALL:
            meta["warning"] = (
                f"返回条数达到单次上限 {MAX_ROWS_PER_CALL}，区间内可能有数据被截断，"
                "建议缩小时间范围分段查询。"
            )
        return json.dumps({"ok": True, "meta": meta, "data": rows}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def get_kline_recent(
    symbol: str,
    frequency: str,
    count: int = 100,
    end_time: str = "",
    adjust: str = "none",
    fields: str = "",
) -> str:
    """获取最近 N 条 K 线（不需要算时间区间，适合「最近走势」类问题）。

    Args:
        symbol: 标的代码，如 `600000` 或 `SZSE.000001`。
        frequency: 同上，`1d` / `60s` / `300s` / `900s` / `1800s` / `3600s` / `tick`。
        count: 条数，正整数，默认 100，建议不超过 5000。
        end_time: 可选，往回取数的截止时间；留空表示截止到当前。
        adjust: `none` / `prev` / `post`。
        fields: 可选字段列表。

    Returns:
        JSON：data 为 K 线列表（按时间升序）。
    """
    try:
        sym = normalize_symbol(symbol)
        freq = validate_frequency(frequency)
        adj = resolve_adjust(adjust)
        if count <= 0:
            raise ValueError("count 必须是正整数。")
        if count > MAX_ROWS_PER_CALL:
            raise ValueError(f"count 不能超过 {MAX_ROWS_PER_CALL}。")
        rows = await get_client().ahistory_n(
            symbol=sym,
            frequency=freq,
            count=count,
            end_time=end_time.strip() or None,
            fields=fields.strip() or None,
            adjust=adj,
        )
        meta = {
            "symbol": sym,
            "frequency": freq,
            "adjust": adj,
            "requested": count,
            "count": len(rows),
        }
        return json.dumps({"ok": True, "meta": meta, "data": rows}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def normalize_symbol_code(code: str) -> str:
    """把标的代码转换成掘金要求的 `EXCHANGE.CODE` 格式。

    当你不确定用户给的代码属于哪个交易所时，先用它转一次。

    Args:
        code: 任意常见写法，如 `600000` / `sh600000` / `600000.SH` / `SHSE.600000`。

    Returns:
        JSON：data 为规范化后的代码。
    """
    try:
        return _ok({"input": code, "normalized": normalize_symbol(code)})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
async def list_supported_frequencies() -> str:
    """列出本服务支持的 K 线频率及其数据范围说明。

    在不确定该用哪个 frequency 参数时先调用它。
    """
    return _ok(
        {
            "frequencies": SUPPORTED_FREQUENCIES,
            "adjust_options": {
                "none": "不复权",
                "prev": "前复权（做技术分析推荐）",
                "post": "后复权",
            },
            "limits": {
                "max_rows_per_call": MAX_ROWS_PER_CALL,
                "minute_history": "分钟线历史约从 2016 年起",
                "tick_history": "tick 历史仅最近约 3 个月",
                "unsupported": "掘金不支持周线/月线/年线，可用日线本地合成",
            },
        }
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip() or "stdio"
    if transport not in ("stdio", "sse", "streamable-http"):
        print(f"未知的 MCP_TRANSPORT: {transport}", file=sys.stderr)
        sys.exit(2)
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
