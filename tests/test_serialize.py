"""serialize 模块的单元测试。"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from myquant_mcp.serialize import (
    records_to_jsonable,
    to_jsonable,
    trim_empty_strings,
)


class TestToJsonable:
    def test_datetime_to_isoformat(self):
        value = dt.datetime(2026, 10, 21, 9, 31, 0)
        assert to_jsonable(value) == "2026-10-21T09:31:00"

    def test_date_to_isoformat(self):
        assert to_jsonable(dt.date(2026, 10, 21)) == "2026-10-21"

    def test_nan_becomes_none(self):
        assert to_jsonable(float("nan")) is None

    def test_inf_becomes_none(self):
        assert to_jsonable(float("inf")) is None
        assert to_jsonable(float("-inf")) is None

    def test_float_passthrough(self):
        assert to_jsonable(3.14) == 3.14

    def test_bool_not_treated_as_int(self):
        assert to_jsonable(True) is True
        assert to_jsonable(False) is False

    def test_nested_structure(self):
        value = {"a": [1, {"b": dt.date(2026, 1, 2)}], "c": float("nan")}
        assert to_jsonable(value) == {"a": [1, {"b": "2026-01-02"}], "c": None}

    def test_numpy_scalar_if_available(self):
        np = pytest.importorskip("numpy")
        assert to_jsonable(np.int64(42)) == 42
        assert to_jsonable(np.float64(1.5)) == 1.5

    def test_bytes_decoded(self):
        assert to_jsonable(b"abc") == "abc"


class TestRecordsToJsonable:
    def test_none_returns_empty(self):
        assert records_to_jsonable(None) == []

    def test_empty_list(self):
        assert records_to_jsonable([]) == []

    def test_list_of_dicts(self):
        rows = [{"a": 1}, {"a": 2}]
        assert records_to_jsonable(rows) == [{"a": 1}, {"a": 2}]

    def test_dict_wrapped_in_list(self):
        assert records_to_jsonable({"a": 1}) == [{"a": 1}]

    def test_dataframe_if_available(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame(
            [
                {"symbol": "SHSE.600000", "eob": dt.datetime(2026, 10, 21), "close": 9.66},
            ]
        )
        out = records_to_jsonable(df)
        assert len(out) == 1
        assert out[0]["symbol"] == "SHSE.600000"
        assert out[0]["eob"] == "2026-10-21T00:00:00"

    def test_non_dict_rows_wrapped(self):
        assert records_to_jsonable([1, 2]) == [{"value": 1}, {"value": 2}]

    def test_datetime_in_rows_converted(self):
        rows = [{"eob": dt.datetime(2026, 10, 21, 15, 0), "close": 1.0}]
        out = records_to_jsonable(rows)
        assert out[0]["eob"] == "2026-10-21T15:00:00"


class TestTrimEmptyStrings:
    def test_removes_empty_string(self):
        assert trim_empty_strings({"a": 1, "b": ""}) == {"a": 1}

    def test_keeps_zero(self):
        # 0 可能是真实的成交量/持仓量，不能删
        assert trim_empty_strings({"volume": 0}) == {"volume": 0}

    def test_keeps_none(self):
        assert trim_empty_strings({"a": None}) == {"a": None}
