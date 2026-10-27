import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from astrbot_plugin_krypton.jev import JevJudge, JevJudgmentError, JevSettings


@pytest.mark.parametrize("suffix", ["", "/v1/", "/v1/systemone"])
def test_jev_endpoint_and_model_configuration(suffix):
    settings = JevSettings.from_config(
        {
            "typesafe_api_base_url": " https://example.test" + suffix + " ",
            "typesafe_model_id": " custom-jev ",
        }
    )
    assert settings.api_url == "https://example.test/v1/systemone"
    assert settings.model == "custom-jev"
    assert JevSettings.from_config({"typesafe_model_id": "  "}).model == "jev-latest"
    assert not JevSettings.from_config({}).enabled


async def test_jev_request_contract_and_session_cleanup(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    received = []

    async def endpoint(request):
        received.append((request.headers.get("Authorization"), await request.json()))
        return web.json_response(
            {"answers": {"engage": {"type": "noul", "noul": 0.82}}}
        )

    app = web.Application()
    app.router.add_post("/v1/systemone", endpoint)
    async with TestServer(app) as server:
        judge = JevJudge()
        settings = JevSettings(
            api_key="test-only-key", api_base_url=str(server.make_url("/"))
        )
        try:
            state = {"recent_messages": [{"sender": "甲", "text": "聊游戏"}]}
            assert await judge.score(settings, state) == 0.82
            assert judge._session.trust_env
            assert received[0][0] == "Bearer test-only-key"
            body = received[0][1]
            assert body["state"] == state
            assert body["model"] == "jev-latest"
            assert body["questions"]["engage"]["type"] == "noul"
        finally:
            await judge.close()
        assert judge._session.closed
        with pytest.raises(JevJudgmentError):
            await judge.score(settings, {})


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"type": "choice", "choice": "yes"},
        {"type": "noul", "noul": True},
        {"type": "noul", "noul": 1.2},
        {"type": "noul", "noul": "0.9"},
    ],
)
async def test_jev_rejects_invalid_answers(answer, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def endpoint(request):
        return web.json_response({"answers": {"engage": answer}})

    app = web.Application()
    app.router.add_post("/v1/systemone", endpoint)
    async with TestServer(app) as server:
        judge = JevJudge()
        try:
            with pytest.raises(JevJudgmentError):
                await judge.score(
                    JevSettings(api_base_url=str(server.make_url("/"))), {}
                )
        finally:
            await judge.close()


async def test_jev_http_failure_and_timeout_are_explicit(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def unavailable(request):
        return web.Response(status=429, text="service details")

    async def slow(request):
        await asyncio.sleep(0.1)
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/fail/v1/systemone", unavailable)
    app.router.add_post("/slow/v1/systemone", slow)
    async with TestServer(app) as server:
        judge = JevJudge()
        try:
            with pytest.raises(JevJudgmentError, match="HTTP 429"):
                await judge.score(
                    JevSettings(api_base_url=str(server.make_url("/fail"))), {}
                )
            with pytest.raises(JevJudgmentError):
                await judge.score(
                    JevSettings(
                        api_base_url=str(server.make_url("/slow")), timeout=0.01
                    ),
                    {},
                )
        finally:
            await judge.close()


async def test_jev_uses_environment_proxy_and_honors_bypass(monkeypatch):
    proxy_requests = []

    async def proxy(request):
        proxy_requests.append(request.host)
        return web.json_response({"answers": {"engage": {"type": "noul", "noul": 0.9}}})

    async def direct(request):
        return web.json_response({"answers": {"engage": {"type": "noul", "noul": 0.2}}})

    proxy_app, direct_app = web.Application(), web.Application()
    proxy_app.router.add_route("*", "/{path:.*}", proxy)
    direct_app.router.add_post("/v1/systemone", direct)
    async with (
        TestServer(proxy_app) as proxy_server,
        TestServer(direct_app) as direct_server,
    ):
        monkeypatch.setenv("http_proxy", str(proxy_server.make_url("/")))
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setenv("NO_PROXY", "")
        judge = JevJudge()
        try:
            assert (
                await judge.score(JevSettings(api_base_url="http://jev.invalid"), {})
                == 0.9
            )
            assert proxy_requests == ["jev.invalid"]
            monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
            assert (
                await judge.score(
                    JevSettings(api_base_url=str(direct_server.make_url("/"))), {}
                )
                == 0.2
            )
            assert proxy_requests == ["jev.invalid"]
        finally:
            await judge.close()
