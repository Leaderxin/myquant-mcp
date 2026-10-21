"""client 模块的单元测试：符号规范化、参数校验、SDK 调用封装（mock）。"""

from __future__ import annotations

import datetime as dt
import sys
import types

import pytest

from myquant_mcp import client as client_mod
from myquant_mcp.client import (
    DEFAULT_SERV_ADDR,
    GMClient,
    GMConfig,
    GMConfigError,
    GMConnectionError,
    GMDataError,
    normalize_symbol,
    reset_connection,
    resolve_adjust,
    validate_frequency,
)


@pytest.fixture(autouse=True)
def _clear_connection_state():
    """每个用例前后都重置类级连接状态，避免互相污染。"""
    reset_connection()
    yield
    reset_connection()


# ---------------------------------------------------------------------------
# normalize_symbol
# ---------------------------------------------------------------------------


class TestNormalizeSymbol:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SHSE.600000", "SHSE.600000"),
            ("shse.600000", "SHSE.600000"),
            ("sh600000", "SHSE.600000"),
            ("SH600000", "SHSE.600000"),
            ("600000.SH", "SHSE.600000"),
            ("600000.SHSE", "SHSE.600000"),
            ("600000", "SHSE.600000"),
            ("510050", "SHSE.510050"),   # 5 开头 -> 沪市基金
            ("000001", "SZSE.000001"),   # 0 开头 -> 深市
            ("300750", "SZSE.300750"),   # 3 开头 -> 创业板（深市）
            ("sz000001", "SZSE.000001"),
            ("SZSE.000001", "SZSE.000001"),
            (" 600000 ", "SHSE.600000"),
        ],
    )
    def test_valid_inputs(self, raw, expected):
        assert normalize_symbol(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", None, "abc", "900000", "6"])
    def test_invalid_inputs(self, raw):
        with pytest.raises(GMDataError):
            normalize_symbol(raw)

    def test_unknown_exchange_prefix(self):
        with pytest.raises(GMDataError):
            normalize_symbol("HK.00700")


# ---------------------------------------------------------------------------
# validate_frequency / resolve_adjust
# ---------------------------------------------------------------------------


class TestFrequencyAndAdjust:
    @pytest.mark.parametrize("freq", ["1d", "60s", "300s", "900s", "1800s", "3600s", "tick"])
    def test_supported(self, freq):
        assert validate_frequency(freq) == freq

    def test_case_insensitive(self):
        assert validate_frequency("1D") == "1d"
        assert validate_frequency("TICK") == "tick"

    @pytest.mark.parametrize("freq", ["1w", "1M", "5s", "", "1m"])
    def test_unsupported(self, freq):
        with pytest.raises(GMDataError) as ei:
            validate_frequency(freq)
        assert "不支持的频率" in str(ei.value)

    @pytest.mark.parametrize(
        "value,expected",
        [(None, 0), ("none", 0), ("prev", 1), ("post", 2), (0, 0), (1, 1), (2, 2)],
    )
    def test_resolve_adjust(self, value, expected):
        assert resolve_adjust(value) == expected

    @pytest.mark.parametrize("value", ["abc", 9, "PRE"])
    def test_resolve_adjust_invalid(self, value):
        with pytest.raises(GMDataError):
            resolve_adjust(value)


# ---------------------------------------------------------------------------
# GMConfig
# ---------------------------------------------------------------------------


class TestGMConfig:
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("GM_TOKEN", "tok123")
        monkeypatch.setenv("GM_SERV_ADDR", "192.168.1.10:7001")
        cfg = GMConfig.from_env()
        assert cfg.token == "tok123"
        assert cfg.serv_addr == "192.168.1.10:7001"

    def test_default_serv_addr(self, monkeypatch):
        monkeypatch.delenv("GM_SERV_ADDR", raising=False)
        monkeypatch.setenv("GM_TOKEN", "tok")
        assert GMConfig.from_env().serv_addr == DEFAULT_SERV_ADDR

    def test_missing_token_raises(self):
        with pytest.raises(GMConfigError) as ei:
            GMConfig(token="").validate()
        assert "GM_TOKEN" in str(ei.value)

    def test_bad_serv_addr(self):
        with pytest.raises(GMConfigError) as ei:
            GMConfig(token="tok", serv_addr="no-port").validate()
        assert "host:port" in str(ei.value)


# ---------------------------------------------------------------------------
# GMClient（用假的 gm.api 模块替换真 SDK）
# ---------------------------------------------------------------------------


class FakeGMAPI:
    """模拟 gm.api 的行为，记录调用参数。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.raise_on_token: Exception | None = None

    def set_serv_addr(self, addr):
        self.calls.append(("set_serv_addr", {"addr": addr}))

    def set_token(self, token):
        self.calls.append(("set_token", {"token": token}))
        if self.raise_on_token:
            raise self.raise_on_token

    def current(self, **kwargs):
        self.calls.append(("current", kwargs))
        return [
            {
                "symbol": "SHSE.600000",
                "price": 9.66,
                "created_at": dt.datetime(2026, 10, 21, 11, 20, 18),
            }
        ]

    def history(self, **kwargs):
        self.calls.append(("history", kwargs))
        return [
            {"symbol": "SHSE.600000", "eob": dt.datetime(2026, 10, 21), "close": 9.66, "bad": ""}
        ]

    def history_n(self, **kwargs):
        self.calls.append(("history_n", kwargs))
        return []


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeGMAPI()
    module = types.SimpleNamespace(api=api)
    monkeypatch.setitem(sys.modules, "gm", module)
    monkeypatch.setitem(sys.modules, "gm.api", api)
    return api


class TestGMClient:
    def test_connect_sets_addr_and_token_once(self, fake_api):
        c = GMClient(GMConfig(token="tok", serv_addr="127.0.0.1:7001"))
        c.connect()
        c.connect()  # 幂等
        names = [n for n, _ in fake_api.calls]
        assert names.count("set_serv_addr") == 1
        assert names.count("set_token") == 1
        assert fake_api.calls[0] == ("set_serv_addr", {"addr": "127.0.0.1:7001"})
        assert fake_api.calls[1] == ("set_token", {"token": "tok"})

    def test_current_serializes_datetime(self, fake_api):
        c = GMClient(GMConfig(token="tok"))
        rows = c.current(["SHSE.600000"])
        assert rows[0]["created_at"] == "2026-10-21T11:20:18"

    def test_history_strips_empty_string_fields(self, fake_api):
        c = GMClient(GMConfig(token="tok"))
        rows = c.history(
            symbol="SHSE.600000",
            frequency="1d",
            start_time="2026-10-01",
            end_time="2026-10-21",
        )
        assert "bad" not in rows[0]
        assert rows[0]["close"] == 9.66

    def test_history_passes_adjust_and_frequency(self, fake_api):
        c = GMClient(GMConfig(token="tok"))
        c.history(
            symbol="SHSE.600000",
            frequency="60s",
            start_time="2026-10-20",
            end_time="2026-10-21",
            adjust=1,
        )
        call = [kw for name, kw in fake_api.calls if name == "history"][0]
        assert call["frequency"] == "60s"
        assert call["adjust"] == 1
        assert call["df"] is False

    def test_missing_token_raises_on_connect(self, fake_api):
        c = GMClient(GMConfig(token=""))
        with pytest.raises(GMConfigError):
            c.connect()

    def test_auth_error_translated(self, fake_api):
        fake_api.raise_on_token = Exception("invalid token")
        c = GMClient(GMConfig(token="bad"))
        with pytest.raises(client_mod.GMAuthError) as ei:
            c.connect()
        assert "鉴权失败" in str(ei.value)

    def test_connection_error_translated(self, fake_api):
        fake_api.raise_on_token = Exception("connect refused")
        c = GMClient(GMConfig(token="tok"))
        with pytest.raises(GMConnectionError) as ei:
            c.connect()
        assert "掘金终端" in str(ei.value)

    def test_chinese_connection_error_translated(self, fake_api):
        """掘金真实返回的中文错误必须识别为连接错误，而非泛化错误。"""
        fake_api.raise_on_token = Exception(
            '{"status": 1001, "message": "无法连接到终端服务", "function": "current"}'
        )
        c = GMClient(GMConfig(token="tok"))
        with pytest.raises(GMConnectionError) as ei:
            c.connect()
        assert "GM_SERV_ADDR" in str(ei.value)

    def test_chinese_auth_error_translated(self, fake_api):
        fake_api.raise_on_token = Exception('{"status": 1002, "message": "密钥无效"}')
        c = GMClient(GMConfig(token="bad"))
        with pytest.raises(client_mod.GMAuthError):
            c.connect()

    def test_auth_beats_conn_when_only_auth_keywords(self, fake_api):
        """只命中鉴权词时不应误判成连接错误。"""
        fake_api.raise_on_token = Exception("invalid token provided")
        c = GMClient(GMConfig(token="bad"))
        with pytest.raises(client_mod.GMAuthError):
            c.connect()

    async def test_acurrent_runs_in_thread(self, fake_api):
        c = GMClient(GMConfig(token="tok"))
        rows = await c.acurrent(["SHSE.600000"])
        assert rows[0]["price"] == 9.66

    async def test_aping_returns_probe(self, fake_api):
        c = GMClient(GMConfig(token="tok"))
        info = await c.aping()
        assert info["connected"] is True
        assert info["probe_symbol"] == "SHSE.000001"
        assert info["probe_rows"] == 1
