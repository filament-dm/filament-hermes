"""Stage 1 reports where the agent runs, and never fails the connect doing so.

After a successful ``get_self`` the adapter calls ``report_runtime`` once with
the harness, the plugin version and the machine, so the owner and the agents
admin can see which plugin version each Hermes agent runs. A server without
the tool, a refusal or a network error is logged and startup goes on.

The adapter is loaded with the Hermes stubs ``test_media_send`` installs.
"""

import asyncio
import importlib.util
import logging
import types
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_media_send", Path(__file__).with_name("test_media_send.py")
)
_media_send = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_media_send)
adapter = _media_send.adapter
_Adapter = adapter.FCMFilamentAdapter

_SELF = {
    "result": {
        "content": [
            {
                "type": "text",
                "text": (
                    '{"mxid": "@agent:s", "display_name": "Agent",'
                    ' "owner": {"user_id": "@owner:s", "display_name": "Owner"}}'
                ),
            }
        ]
    }
}


class _Api:
    _mcp_url = "http://server/mcp/agents"
    mcp_url = _mcp_url
    token_hash = "ab12"
    parse_tool_result = staticmethod(adapter.FilamentAPI.parse_tool_result)

    def __init__(self, report_result=None, report_raises=None):
        self.reports = []
        self._report_result = report_result
        self._report_raises = report_raises

    async def initialize(self):
        return {"result": {"instructions": ""}}

    async def get_self(self):
        return _SELF

    async def report_runtime(self, runtime):
        self.reports.append(runtime)
        if self._report_raises is not None:
            raise self._report_raises
        return self._report_result


def _stub(api):
    async def no_invites():
        return None

    stub = types.SimpleNamespace(
        _filament_api=api,
        _credentials=types.SimpleNamespace(save_connection=lambda *a, **k: None),
        _reserved=False,
        _auth_refused=None,
        _server_guidance_text=_Adapter._server_guidance_text,
        _accept_pending_invites=no_invites,
    )
    stub._report_runtime = types.MethodType(_Adapter._report_runtime, stub)
    return stub


@pytest.fixture(autouse=True)
def _no_skill_write(monkeypatch):
    monkeypatch.setattr(adapter, "write_server_guide_skill", lambda text: True)


def test_stage_1_reports_hermes_its_version_and_the_machine(monkeypatch):
    monkeypatch.setattr(adapter.socket, "gethostname", lambda: "pablos-mbp.local")
    api = _Api(report_result={"result": {"content": [{"type": "text", "text": "{}"}]}})

    assert asyncio.run(_Adapter._initialize_api(_stub(api))) is True

    assert api.reports == [
        {
            "harness": "hermes",
            "version": adapter.PLUGIN_VERSION,
            "machine": "pablos-mbp",
        }
    ]


def test_an_unknown_machine_is_left_out(monkeypatch):
    monkeypatch.setattr(adapter.socket, "gethostname", lambda: "")
    assert adapter._runtime_report() == {
        "harness": "hermes",
        "version": adapter.PLUGIN_VERSION,
    }


@pytest.mark.parametrize(
    ("api", "level"),
    [
        # A server that predates the tool.
        (
            _Api(
                report_result={
                    "error": {"code": -32601, "message": "Unknown tool: report_runtime"}
                }
            ),
            logging.INFO,
        ),
        # A refusal reported as a tool error.
        (
            _Api(
                report_result={
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "invalid arguments"}],
                    }
                }
            ),
            logging.WARNING,
        ),
        (_Api(report_result={"error": "HTTP 502"}), logging.WARNING),
        (_Api(report_raises=OSError("connection reset")), logging.WARNING),
    ],
)
def test_a_failed_report_is_logged_once_and_startup_goes_on(api, level, caplog):
    caplog.set_level(logging.INFO, logger="gateway.filament_fcm")

    assert asyncio.run(_Adapter._initialize_api(_stub(api))) is True

    assert len(api.reports) == 1  # no retry
    about_runtime = [r for r in caplog.records if "runtime" in r.getMessage().lower()]
    assert [r.levelno for r in about_runtime] == [level]
