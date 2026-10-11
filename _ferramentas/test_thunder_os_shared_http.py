"""Actual existing panel Handler over ephemeral loopback, synthetic auth/data only."""
import copy
import http.client
import json
import os
import socket
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_thunder_os_shared_preparation as fixtures
from thunder_os.shared_preparation import SharedPrincipal, SharedPreparation

# Source-only import dependency; no installed/vendor ledger or database call.
_had_toques = "toques" in sys.modules
if not _had_toques:
    sys.modules["toques"] = SimpleNamespace(ErroBanco=RuntimeError)
try:
    import painel_servidor as panel
finally:
    if not _had_toques:
        sys.modules.pop("toques", None)

ORIGINAL_SOCKET = socket.socket
ORIGINAL_CONNECT = socket.create_connection


class LoopbackSocket(ORIGINAL_SOCKET):
    def bind(self, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("non-loopback bind forbidden")
        return super().bind(address)

    def connect(self, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("provider socket forbidden")
        return super().connect(address)


def loopback_connect(address, *args, **kwargs):
    if address[0] not in {"127.0.0.1", "::1"}:
        raise AssertionError("provider connection forbidden")
    return ORIGINAL_CONNECT(address, *args, **kwargs)


class SharedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SharedPreparationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Override only the fixture's blanket socket ban with a loopback-only ban.
        for target, value in (("socket.socket", LoopbackSocket), ("socket.create_connection", loopback_connect)):
            guard = patch(target, value)
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {"THUNDER_EXTERNAL_EFFECTS_ENABLED": "false",
            "THUNDER_OS_ACTORS": "ygor,hermes,claude,codex", "THUNDER_OS_SOURCE_SHA": ""}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.Handler)
        self.server.banco = SimpleNamespace()  # No operational Banco methods used.
        self.server.thunder_service = self.fixture.service
        self.server.thunder_nonces = {}
        self.server.thunder_hermes_credential = "synthetic-existing-credential"
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.server.thunder_origin = self.origin
        self.principals = {actor: self.fixture.principals[actor] for actor in ("hermes", "codex")}
        self.configure()
        self.thread = threading.Thread(target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def configure(self):
        return panel.configure_shared_server(self.server, self.fixture.service,
            factory=lambda service: (self.fixture.core, self.principals))

    def headers(self, actor="codex"):
        headers = {"X-Authenticated-User": actor, "Origin": self.origin}
        if actor == "hermes": headers["X-Thunder-Agent-Credential"] = "synthetic-existing-credential"
        return headers

    def http(self, method, path, *, actor="codex", body=None, headers=None):
        values = self.headers(actor) if actor is not None else {}
        values.update(headers or {})
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        if raw is not None: values["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=values)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def session(self, actor="codex"):
        status, value = self.http("GET", "/api/os/v1/session", actor=actor)
        self.assertEqual(200, status)
        return value["nonce"]

    def post(self, body, *, actor="codex", nonce=None, extra=None):
        return self.http("POST", "/api/os/v1/shared/preparations", actor=actor, body=body,
            headers={"X-Thunder-Nonce": nonce or self.session(actor), **(extra or {})})

    def test_http_context_preparation_status_and_replay(self):
        status, context = self.http("GET", "/api/os/v1/shared/context?account=acme")
        self.assertEqual(200, status)
        self.assertEqual("1.0.0", context["context"]["package_version"])
        self.assertFalse(context["context"]["send_authorized"])
        self.assertNotIn("PRIVATE", json.dumps(context))
        status, first = self.post(self.fixture.envelope())
        self.assertEqual(201, status)
        status, other = self.post(self.fixture.envelope("telegram", account="acme ltda"), actor="hermes")
        self.assertEqual(201, status)
        self.assertEqual(first["id"], other["id"])
        self.assertEqual("succeeded", self.fixture.worker().run_once()["status"])
        status, result = self.http("GET", f"/api/os/v1/shared/work/{first['id']}", actor="hermes")
        self.assertEqual(200, status)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual({"codex", "hermes"}, {row["actor"] for row in result["origins"]})
        status, replay = self.post(self.fixture.envelope())
        self.assertEqual(201, status)
        self.assertEqual(first["id"], replay["id"])
        self.fixture.worker().run_once()
        self.assertEqual(1, len(self.fixture.calls))
        self.assertEqual([], self.fixture.repo.effects())

    def test_http_existing_auth_nonce_and_scopes_fail_closed(self):
        self.assertEqual(401, self.http("GET", "/api/os/v1/shared/context?account=acme", actor=None)[0])
        self.assertEqual(401, self.http("GET", "/api/os/v1/shared/context?account=acme", actor="unknown")[0])
        self.assertEqual(403, self.http("GET", "/api/os/v1/shared/context?account=acme", actor="claude")[0])
        self.assertEqual(401, self.http("GET", "/api/os/v1/shared/context?account=acme", actor="hermes",
            headers={"X-Thunder-Agent-Credential": "forged"})[0])
        nonce = self.session()
        self.assertEqual(401, self.post(self.fixture.envelope(), nonce=nonce, extra={"Origin": "https://forged.example"})[0])
        self.assertEqual(401, self.post(self.fixture.envelope(), nonce=nonce)[0])
        self.assertEqual(401, self.post(self.fixture.envelope(), extra={"X-Thunder-Actor": "ygor"})[0])
        self.principals["codex"] = SharedPrincipal("ygor", "codex", "local", "existing", frozenset({"read"}))
        self.configure()
        self.assertEqual(200, self.http("GET", "/api/os/v1/shared/context?account=acme")[0])
        self.assertEqual(403, self.post(self.fixture.envelope())[0])
        self.assertEqual([], self.fixture.repo.list_work())
        self.assertEqual([], self.fixture.repo.list_state("identity"))

    def test_http_unknown_capabilities_and_forged_fields(self):
        for capability in ("send", "spend", "review", "unknown"):
            self.assertEqual(422, self.post(self.fixture.envelope(capability=capability))[0])
        for field in ("actor", "approved", "receipt", "model", "command", "brain_path"):
            self.assertEqual(422, self.post(self.fixture.envelope(**{field: "forged"}))[0])
        self.assertEqual([], self.fixture.repo.list_work())
        self.assertEqual(0, len(self.fixture.calls))

    def test_http_disabled_without_composition(self):
        panel.configure_shared_server(self.server, self.fixture.service)
        self.assertEqual(503, self.http("GET", "/api/os/v1/shared/context?account=acme")[0])
        self.assertEqual(503, self.post(self.fixture.envelope())[0])
        self.assertEqual([], self.fixture.repo.list_work())

    def test_composition_rejects_unknown_actor_scope_and_wrong_core(self):
        for binding in (
            (self.fixture.core, {"unknown": self.fixture.principals["codex"]}),
            (self.fixture.core, {"codex": self.fixture.principals["hermes"]}),
            (self.fixture.core, {"codex": SharedPrincipal("ygor", "codex", "local", "existing", frozenset({"send"}))}),
            (object(), self.principals),
        ):
            with self.assertRaises(ValueError):
                panel.configure_shared_server(self.server, self.fixture.service, factory=lambda service: binding)
            self.assertIsNone(self.server.thunder_shared_adapters)
        self.assertEqual([], self.fixture.repo.list_state("identity"))

    def test_existing_main_uses_same_explicit_composition(self):
        captured = SimpleNamespace(server_port=0, serve_forever=Mock())
        def factory(service):
            core = SharedPreparation(service, context=self.fixture.context,
                account_reader=lambda _: copy.deepcopy(self.fixture.snapshot))
            return core, self.principals
        with patch.object(panel, "Banco", return_value=SimpleNamespace(arquivo=self.fixture.repo.path)), \
             patch.object(panel, "Repository", return_value=self.fixture.repo), \
             patch.object(panel, "KnowledgeStore", return_value=self.fixture.store), \
             patch.object(panel, "ThreadingHTTPServer", return_value=captured), \
             patch.object(panel, "configure_connectsafely_server"), \
             patch.object(panel, "SEQDConsole"), patch.object(panel, "HubSpotConsole"):
            self.assertEqual(0, panel.main(["servir"], shared_factory=factory))
        self.assertEqual({"codex", "hermes"}, set(captured.thunder_shared_adapters))
        captured.serve_forever.assert_called_once_with()
        self.assertEqual([], self.fixture.repo.effects())

    def test_http_schema_methods_and_paused_work(self):
        for query in ("account=acme&account=acme", "account=acme&extra=1", "other=acme", "account"):
            self.assertEqual(400, self.http("GET", "/api/os/v1/shared/context?" + query)[0])
        self.assertEqual(405, self.http("PATCH", "/api/os/v1/shared/context", body={})[0])
        self.assertEqual(404, self.http("GET", "/api/os/v1/shared/send")[0])
        self.assertEqual(404, self.http("GET", "/api/os/v1/shared/work/work-" + "1" * 32)[0])
        self.fixture.repo.put_state("agent_control", "global", {"enabled": False, "paused": True})
        status, item = self.post(self.fixture.envelope())
        self.assertEqual(201, status)
        self.assertEqual("needs_human", item["status"])
        status, state = self.http("GET", f"/api/os/v1/shared/work/{item['id']}")
        self.assertEqual(200, status)
        self.assertEqual("agent_paused", state["blocked_reason"])
        self.assertFalse(self.fixture.repo.get_state("agent_control", "global")["enabled"])
        self.assertEqual(0, len(self.fixture.calls))


if __name__ == "__main__":
    unittest.main()
