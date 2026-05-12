"""Tests for _wfs_get SSL fallback (PR #15 regression)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from requests.exceptions import SSLError

from auvergne_pipeline.ign_routes import _wfs_get


def test_wfs_get_uses_certifi():
    with patch("requests.get") as mock:
        mock.return_value = MagicMock(status_code=200)
        _wfs_get({}, "https://example.com", 30)
        assert mock.call_count == 1
        # First attempt uses certifi (verify != False)
        kwargs = mock.call_args.kwargs
        assert not kwargs.get("verify") == False  # noqa


def test_wfs_get_falls_back_on_ssl_error():
    with patch("requests.get") as mock:
        mock.side_effect = [SSLError("cert verify failed"), MagicMock(status_code=200)]
        _wfs_get({}, "https://example.com", 30)
        assert mock.call_count == 2
        # Second call was the fallback
        assert mock.call_args_list[1].kwargs.get("verify") == False  # noqa
