import logging

import pytest
import requests

from crash_digest import teams

URL = ("https://default123.35.environment.api.powerplatform.com:443/powerautomate/automations/direct/"
       "workflows/abc/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers&sv=1.0&sig=SECRETSIG")


def test_failed_post_never_logs_webhook_path_or_sig(monkeypatch, caplog):
    def proxy_fail(url, **kw):
        raise requests.exceptions.ProxyError(
            f"HTTPSConnectionPool(host='default123.35.environment.api.powerplatform.com', port=443): "
            f"Max retries exceeded with url: {URL.split(':443')[1]} (Caused by ProxyError('Unable to connect to proxy', "
            f"OSError('Tunnel connection failed: 403 Forbidden')))")
    monkeypatch.setattr(teams.requests, "post", proxy_fail)
    caplog.set_level(logging.WARNING)
    with pytest.raises(teams.TeamsPostError) as err:
        teams.post_card(URL, {}, attempts=2, backoff_s=0)
    text = caplog.text + str(err.value)
    assert "SECRETSIG" not in text and "workflows/abc" not in text and "sig=" not in text
    assert "403 Forbidden" in text and "ProxyError" in text   # still useful for debugging
