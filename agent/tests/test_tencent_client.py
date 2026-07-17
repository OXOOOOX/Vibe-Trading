from __future__ import annotations

from unittest.mock import Mock, patch

from backtest.loaders import tencent_client


def test_search_decodes_tencent_unicode_hint_rows() -> None:
    response = Mock()
    response.text = r'v_hint="sh~603738~\u6cf0\u6676\u79d1\u6280~tjkj~GP-A"'
    response.raise_for_status.return_value = None

    with patch.object(tencent_client, "throttled_get", return_value=response):
        rows = tencent_client.search("泰晶")

    assert rows == ["sh~603738~泰晶科技~tjkj~GP-A"]
    response.raise_for_status.assert_called_once_with()


def test_search_rejects_unexpected_javascript() -> None:
    response = Mock()
    response.text = "alert('unexpected')"
    response.raise_for_status.return_value = None

    with patch.object(tencent_client, "throttled_get", return_value=response):
        try:
            tencent_client.search("泰晶")
        except ValueError as exc:
            assert "invalid response" in str(exc)
        else:
            raise AssertionError("invalid Tencent response should fail")
