import asyncio
import os
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_openconnect.py")


@pytest.fixture
def vpn(tmp_path, monkeypatch):
    from vpngatech import vpn

    monkeypatch.setattr(vpn, "OPENCONNECT", str(FAKE))
    monkeypatch.setattr(vpn, "STATE_DIR", tmp_path)
    monkeypatch.setattr(vpn, "LOG_FILE", tmp_path / "openconnect.log")
    vpn.prepare_log()
    return vpn


def run(vpn, password, duo="push", answers=()):
    asked = []
    answers = list(answers)

    async def ask(prompt):
        asked.append(prompt)
        return answers.pop(0) if answers else None

    login = asyncio.run(
        vpn.authenticate("gburdell3", password, "dc-ext-gw.vpn.gatech.edu", duo, ask, lambda m: None)
    )
    return login, asked


def test_duo_push_login(vpn):
    login, asked = run(vpn, "secret")
    assert asked == []
    assert login.cookie == "authcookie=abc&portal=GATECH&user=gburdell3"
    assert login.connect_url == "https://dc-ext-gw.vpn.gatech.edu"
    assert login.fingerprint == "pin-sha256:AAAA"
    assert login.resolve == "dc-ext-gw.vpn.gatech.edu:128.61.110.1"
    log = vpn.LOG_FILE.read_text()
    assert "answering Duo challenge with push" in log
    assert "secret" not in log and "authcookie" not in log


def test_passcode_is_not_logged(vpn):
    run(vpn, "secret", duo="123456")
    assert "123456" not in vpn.LOG_FILE.read_text()


def test_wrong_password(vpn):
    with pytest.raises(vpn.VpnError, match="Authentication failed"):
        run(vpn, "bad")


def test_unknown_prompt_goes_to_user(vpn):
    login, asked = run(vpn, "ask", answers=["blue"])
    assert [p.label for p in asked] == ["Pick a color [red/blue]:"]
    assert asked[0].kind == "other"
    assert login.cookie


def test_cancelled_prompt(vpn):
    with pytest.raises(vpn.VpnError, match="cancelled"):
        run(vpn, "ask")


def test_tunnel_command(vpn):
    login = vpn.Login("c", "https://dc-ext-gw.vpn.gatech.edu", "pin-sha256:AAAA", "h:1.2.3.4")
    cmd = vpn.tunnel_command(login, "gburdell3")
    assert cmd[:4] == ["sudo", "-n", "setsid", "-w"]
    assert "--cookie-on-stdin" in cmd and "--background" in cmd
    assert "--servercert=pin-sha256:AAAA" in cmd and "--resolve=h:1.2.3.4" in cmd
    assert cmd[-1] == "https://dc-ext-gw.vpn.gatech.edu"
    assert "c" not in cmd
