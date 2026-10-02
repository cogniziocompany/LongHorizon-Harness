"""fc-H3a: fleet channel configuration and signed handshake."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from lh_harness.fleet.channel import (
    CHANNEL_PATH,
    ChannelConfig,
    channel_url,
    handshake_headers,
    load_config,
    sign,
)

KEY = "test-device-key"


def test_channel_url_uses_host_of_fleet_url_and_wss():
    assert channel_url("https://fleet.easybutt0n.ai") == "wss://fleet.easybutt0n.ai/harness/channel"
    assert channel_url("https://fleet.easybutt0n.ai/some/path/") == "wss://fleet.easybutt0n.ai/harness/channel"
    assert channel_url("http://10.0.0.5:8080") == "wss://10.0.0.5:8080/harness/channel"
    assert CHANNEL_PATH == "/harness/channel"


def test_channel_url_rejects_missing_host():
    with pytest.raises(ValueError):
        channel_url("not a url")


def test_sign_is_hmac_sha256_of_host_dot_ts():
    expected = hmac.new(KEY.encode(), b"ct110.1700000000", hashlib.sha256).hexdigest()
    assert sign(KEY, "ct110", 1700000000) == expected


def test_handshake_headers_are_consistent():
    headers = handshake_headers("ct110", KEY, now=1700000000.9)
    assert headers["X-Fleet-Host"] == "ct110"
    assert headers["X-Fleet-Ts"] == "1700000000"
    assert headers["X-Fleet-Signature"] == sign(KEY, "ct110", 1700000000)
    assert set(headers) == {"X-Fleet-Host", "X-Fleet-Ts", "X-Fleet-Signature"}


def test_handshake_headers_default_to_now():
    headers = handshake_headers("ct110", KEY)
    assert abs(int(headers["X-Fleet-Ts"]) - __import__("time").time()) < 5


def test_load_config_disabled_without_url():
    assert load_config({}) is None
    assert load_config({"LH_HARNESS_FLEET_KEY": KEY}) is None


def test_load_config_disabled_by_channel_flag():
    env = {"LH_HARNESS_FLEET_URL": "https://fleet.example", "LH_HARNESS_FLEET_KEY": KEY,
           "LH_HARNESS_FLEET_CHANNEL": "0"}
    assert load_config(env) is None


def test_load_config_disabled_without_key():
    assert load_config({"LH_HARNESS_FLEET_URL": "https://fleet.example"}) is None


def test_load_config_enabled():
    env = {"LH_HARNESS_FLEET_URL": "https://fleet.example", "LH_HARNESS_FLEET_KEY": KEY,
           "LH_HARNESS_FLEET_NODE": "ct110"}
    cfg = load_config(env)
    assert cfg == ChannelConfig(url="wss://fleet.example/harness/channel", node="ct110", key=KEY)


def test_load_config_node_falls_back_to_hostname(monkeypatch):
    monkeypatch.setattr("lh_harness.fleet.channel.socket.gethostname", lambda: "box1")
    cfg = load_config({"LH_HARNESS_FLEET_URL": "https://fleet.example", "LH_HARNESS_FLEET_KEY": KEY})
    assert cfg is not None and cfg.node == "box1"


def test_config_repr_hides_key():
    cfg = ChannelConfig(url="wss://x/harness/channel", node="n", key=KEY)
    assert KEY not in repr(cfg)
