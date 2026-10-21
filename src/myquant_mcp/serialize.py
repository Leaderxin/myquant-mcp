"""数据类型转换与序列化。

掘金 SDK 返回的 dict / DataFrame 里混有 datetime、numpy 标量、NaN 等
JSON 无法直接序列化的类型，这里统一转成 MCP 可以安全传输的原生类型。
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any


def to_jsonable(value: Any) -> Any:
    """把单个值转成 JSON 可序列化的原生类型。

    规则：
    - datetime / date -> ISO8601 字符串（带时区则保留）
    - numpy 标量 -> 对应的 Python 原生类型
    - NaN / inf -> None（JSON 不支持，且对下游无意义）
    - bytes -> utf-8 字符串（解码失败则十六进制）
    - dict / list / tuple -> 递归处理
    """
    if value is None:
        return None

    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()

    if isinstance(value, bool):
        return value

    if isinstance(value, (str, int)):
        return value

    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()

    # numpy 标量（np.int64 / np.float64 等）都有 item()
    if hasattr(value, "item") and not isinstance(value, (dict, list, tuple)):
        try:
            return to_jsonable(value.item())
        except (ValueError, AttributeError):
            pass

    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]

    # pandas.Timestamp 等带 isoformat 的对象
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (ValueError, TypeError):
            pass

    return str(value)


def records_to_jsonable(records: Any) -> list[dict[str, Any]]:
    """把掘金返回的 list[dict] 或 DataFrame 统一转成 list[dict]。

    - list[dict]：逐条转换
    - DataFrame：走 to_dict(orient="records") 后再转换
    - None / 空：返回空列表
    """
    if records is None:
        return []

    # duck-typing 判断 DataFrame，避免硬依赖 pandas 类型
    if hasattr(records, "to_dict") and hasattr(records, "columns"):
        try:
            records = records.to_dict(orient="records")
        except (ValueError, TypeError):
            return []

    if isinstance(records, dict):
        return [to_jsonable(records)]

    if not isinstance(records, (list, tuple)):
        return []

    out: list[dict[str, Any]] = []
    for row in records:
        if isinstance(row, dict):
            out.append({str(k): to_jsonable(v) for k, v in row.items()})
        else:
            out.append({"value": to_jsonable(row)})
    return out


def trim_empty_strings(row: dict[str, Any]) -> dict[str, Any]:
    """去掉掘金在字段无效时填充的空字符串，避免把噪音当数据。

    掘金文档说明：代码正确但查询字段含无效字段时，返回的每行里
    无效字段的值为 '' 或 0。这里对空字符串做剔除，0 保留
    （0 可能是真实的成交量/持仓量）。
    """
    return {k: v for k, v in row.items() if v != ""}
