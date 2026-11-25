"""send_image re-uploads a model-supplied image URL. It must never fetch a private
host (SSRF), follow a redirect onto one, or read a local file (exfiltration), and it
must refuse outright when Hermes' url_safety module is unavailable (fail closed)."""

import asyncio
import ipaddress
import json
import sys
import types
from urllib.parse import urlsplit

import httpx
import pytest

from user.adapter import NextcloudTalkUserAdapter, ROOM_TYPE_GROUP
from gateway.config import PlatformConfig

PNG = b"\x89PNG\r\n\x1a\nfake"
# Names a real resolver would map; anything else is "public".
_RESOLVES = {"localhost": "127.0.0.1", "intranet.example": "10.1.2.3", "cdn.example": "93.184.216.34"}


def _fake_is_safe_url(url):
    """Mirrors tools.url_safety.is_safe_url's contract: private, loopback,
    link-local (cloud metadata) and unresolvable-to-public targets are unsafe."""
    host = (urlsplit(url).hostname or "").lower()
    addr = _RESOLVES.get(host, host)
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def _install_url_safety(monkeypatch, handler, checked):
    mod = types.ModuleType("tools.url_safety")

    def is_safe_url(url):
        checked.append(url)
        return _fake_is_safe_url(url)

    def create_ssrf_safe_async_client(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    mod.is_safe_url = is_safe_url
    mod.create_ssrf_safe_async_client = create_ssrf_safe_async_client
    monkeypatch.setitem(sys.modules, "tools.url_safety", mod)


def _adapter():
    adapter = NextcloudTalkUserAdapter(PlatformConfig(extra={
        "server_url": "https://cloud.example.com", "username": "hermes-bot", "app_password": "x",
    }))
    adapter._room_meta["tok1"] = {"token": "tok1", "type": ROOM_TYPE_GROUP, "displayName": "ops"}
    talk_calls = []

    def talk(request):
        talk_calls.append((request.method, request.url.path, request.content))
        if request.url.path.endswith("/shares"):
            return httpx.Response(200, json={"ocs": {"data": {"id": 90}}})
        if request.method == "MKCOL":
            return httpx.Response(405)
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(201, json={"ocs": {"data": {"id": 777}}})

    adapter._http_client = httpx.AsyncClient(base_url="https://cloud.example.com", transport=httpx.MockTransport(talk))
    return adapter, talk_calls


def _never_called(request):
    raise AssertionError(f"fetched {request.url}")


def _posted_as_text(talk_calls, url):
    return [c[0] for c in talk_calls] == ["POST"] and url in json.loads(talk_calls[0][2])["message"]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x.png",
    "http://10.0.0.5/x.png",
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost:8080/x.png",
    "http://intranet.example/x.png",
    "http://[::1]/x.png",
])
def test_private_hosts_are_never_fetched(monkeypatch, url):
    checked = []
    _install_url_safety(monkeypatch, _never_called, checked)
    adapter, talk_calls = _adapter()
    result = asyncio.run(adapter.send_image("tok1", url))
    assert result.success and checked == [url]
    assert _posted_as_text(talk_calls, url), "no bytes uploaded, only the link posted"


def test_redirect_to_private_host_is_refused(monkeypatch):
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    checked = []
    _install_url_safety(monkeypatch, handler, checked)
    adapter, talk_calls = _adapter()
    asyncio.run(adapter.send_image("tok1", "https://cdn.example/x.png"))
    assert fetched == ["https://cdn.example/x.png"]
    assert checked == ["https://cdn.example/x.png", "http://169.254.169.254/latest/meta-data/"]
    assert "PUT" not in [c[0] for c in talk_calls]


@pytest.mark.parametrize("url", ["file:///etc/passwd", "/etc/passwd"])
def test_local_files_are_never_read(monkeypatch, tmp_path, url):
    checked = []
    _install_url_safety(monkeypatch, _never_called, checked)
    adapter, talk_calls = _adapter()
    asyncio.run(adapter.send_image("tok1", url))
    assert checked == [] and "PUT" not in [c[0] for c in talk_calls]


def test_public_url_is_fetched_and_uploaded_after_a_checked_redirect(monkeypatch):
    def handler(request):
        if request.url.path == "/old.png":
            return httpx.Response(301, headers={"location": "/img/x.png"})
        return httpx.Response(200, content=PNG)

    checked = []
    _install_url_safety(monkeypatch, handler, checked)
    adapter, talk_calls = _adapter()
    assert asyncio.run(adapter.send_image("tok1", "https://cdn.example/old.png")).success
    assert checked == ["https://cdn.example/old.png", "https://cdn.example/img/x.png"]
    puts = [c for c in talk_calls if c[0] == "PUT"]
    assert len(puts) == 1 and puts[0][2] == PNG


def test_redirect_loop_gives_up(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"location": "https://cdn.example/again.png"})

    _install_url_safety(monkeypatch, handler, [])
    adapter, talk_calls = _adapter()
    asyncio.run(adapter.send_image("tok1", "https://cdn.example/x.png"))
    assert "PUT" not in [c[0] for c in talk_calls]


def test_missing_url_safety_fails_closed(monkeypatch):
    # None in sys.modules makes the import raise ImportError, like a Hermes without it.
    monkeypatch.setitem(sys.modules, "tools.url_safety", None)
    adapter, talk_calls = _adapter()

    def no_new_clients(*a, **k):
        raise AssertionError("an unchecked client was created")

    monkeypatch.setattr(httpx, "AsyncClient", no_new_clients)
    result = asyncio.run(adapter.send_image("tok1", "https://cdn.example/x.png"))
    assert result.success and _posted_as_text(talk_calls, "https://cdn.example/x.png")
