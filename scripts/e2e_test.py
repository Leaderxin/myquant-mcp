#!/usr/bin/env python3
"""myquant-mcp 端到端实测。

不是直接调 SDK，而是走完整链路：
    MCP client -> stdio -> server.py -> gm SDK -> 掘金终端

用法：
    GM_TOKEN=xxx GM_SERV_ADDR=host.docker.internal:7001 python e2e_test.py
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = "/opt/data/projects/myquant-mcp/server.py"

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"\n{'=' * 70}\n{mark}  {name}\n{'-' * 70}")
    print(detail)


def brief(text, limit=700):
    """截断长输出，保留结构可读性。"""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [截断，共 {len(text)} 字符]"


async def call(session, name, args=None):
    r = await session.call_tool(name, args or {})
    return r.content[0].text if r.content else ""


async def main():
    token = os.environ.get("GM_TOKEN", "").strip()
    addr = os.environ.get("GM_SERV_ADDR", "127.0.0.1:7001").strip()
    if not token:
        print("❌ 没有 GM_TOKEN，无法实测")
        sys.exit(1)

    print(f"目标终端: {addr}")
    print(f"Token 长度: {len(token)}（不打印内容）")

    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env={**os.environ, "GM_TOKEN": token, "GM_SERV_ADDR": addr},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            info = getattr(init, "serverInfo", None)
            server_name = getattr(info, "name", "unknown")
            server_ver = getattr(info, "version", "?")
            record(
                "MCP 握手 + 工具注册",
                len(tools.tools) >= 6,
                f"server={server_name} v{server_ver}  工具数={len(tools.tools)}\n"
                + "\n".join(f"  - {t.name}" for t in tools.tools),
            )

            # 1) 连通性 —— 最关键的一步，失败则后面全部无意义
            raw = await call(session, "ping_gm_terminal")
            data = json.loads(raw)
            record(
                "1. ping_gm_terminal（终端连通性）",
                data.get("ok") is True,
                brief(raw, 1200),
            )
            if not data.get("ok"):
                print("\n⚠️  连通性未通过，后续取数必然失败。错误详情见上。")
                summarize()
                return

            # 2) 实时行情
            raw = await call(session, "get_realtime_quote", {"symbols": "600000,SZSE.000001"})
            data = json.loads(raw)
            ok = data.get("ok") is True and len(data.get("data") or []) > 0
            record("2. get_realtime_quote（实时快照）", ok, brief(raw))

            # 3) 历史 K 线（日线，前复权）
            raw = await call(
                session,
                "get_kline_history",
                {
                    "symbol": "600000",
                    "frequency": "1d",
                    "start_time": "2026-09-01",
                    "end_time": "2026-10-21",
                    "adjust": "prev",
                },
            )
            data = json.loads(raw)
            rows = data.get("data") or []
            ok = data.get("ok") is True and len(rows) > 0
            detail = brief(raw, 500)
            if ok:
                detail += f"\n\n[核验] 条数={len(rows)}  首条eob={rows[0].get('eob')}  末条eob={rows[-1].get('eob')}"
            record("3. get_kline_history（日线前复权）", ok, detail)

            # 4) 最近 N 条（分钟线，验证分钟级可取）
            raw = await call(
                session,
                "get_kline_recent",
                {"symbol": "600000", "frequency": "300s", "count": 20},
            )
            data = json.loads(raw)
            rows = data.get("data") or []
            ok = data.get("ok") is True and len(rows) > 0
            detail = f"meta={data.get('meta')}\n" + brief(raw, 400)
            record("4. get_kline_recent（5分钟线 20 条）", ok, detail)

            # 5) 代码规范化（纯本地，必过）
            raw = await call(session, "normalize_symbol_code", {"code": "000001"})
            data = json.loads(raw)
            ok = data.get("data", {}).get("normalized") == "SZSE.000001"
            record("5. normalize_symbol_code", ok, raw)

            # 6) 频率说明
            raw = await call(session, "list_supported_frequencies")
            data = json.loads(raw)
            record("6. list_supported_frequencies", data.get("ok") is True, brief(raw, 400))

            # 7) 错误路径：故意用非法代码，验证错误结构化
            raw = await call(session, "get_kline_recent", {"symbol": "999999", "frequency": "1d"})
            data = json.loads(raw)
            record(
                "7. 错误路径（非法代码应优雅报错）",
                data.get("ok") is False,
                brief(raw, 500),
            )

            # 8) 错误路径：不支持的频率
            raw = await call(session, "get_kline_recent", {"symbol": "600000", "frequency": "1w"})
            data = json.loads(raw)
            record(
                "8. 错误路径（周线不支持，应提示合成）",
                data.get("ok") is False,
                brief(raw, 400),
            )

    summarize()


def summarize():
    print(f"\n{'=' * 70}\n汇总\n{'-' * 70}")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print(f"\n{passed}/{len(results)} 通过")


if __name__ == "__main__":
    asyncio.run(main())
