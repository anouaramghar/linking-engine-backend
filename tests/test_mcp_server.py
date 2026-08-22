"""The MCP surface: transport auth, protocol behavior, tool execution."""

import json

from fastapi.testclient import TestClient

from app.config import settings

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "linkmesh-tests", "version": "0"},
    },
}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _post(client: TestClient, path: str, payload: dict, key: str | None = None):
    headers = dict(HEADERS)
    if key is not None:
        headers["X-API-Key"] = key
    return client.post(path, content=json.dumps(payload), headers=headers)


def test_mcp_rejects_missing_key(client):
    # The gate runs before the protocol, so even the handshake is refused.
    # With no key configured anywhere this is 503 (auth unconfigured, fail
    # closed); the wrong-key test below covers the plain-401 path.
    response = _post(client, "/mcp/", INITIALIZE)
    assert response.status_code == 503


def test_mcp_rejects_wrong_key(monkeypatch, client):
    monkeypatch.setattr(settings, "api_key", "right-key")
    with TestClient(app=client.app) as fresh:
        response = _post(fresh, "/mcp/", INITIALIZE, key="wrong-key")
        assert response.status_code == 401


def test_mcp_initialize_lists_and_calls_tools(monkeypatch, client, site):
    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    with TestClient(app=client.app) as fresh:
        init = _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        assert init.status_code == 200
        body = init.json()
        assert body["result"]["serverInfo"]["name"] == "LinkMesh"

        tools = _post(
            fresh,
            "/mcp/",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            key="test-mcp-key",
        )
        names = {item["name"] for item in tools.json()["result"]["tools"]}
        assert {"list_sites", "search_queue", "get_evaluation_metrics"} <= names

        called = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "get_queue_counts", "arguments": {}},
            },
            key="test-mcp-key",
        )
        assert called.status_code == 200
        text = called.json()["result"]["content"][0]["text"]
        counts = json.loads(text)
        assert counts["pending"] == 0
        assert "error" not in counts

        denied = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "no_such_tool", "arguments": {}},
            },
            key="test-mcp-key",
        )
        assert denied.status_code == 200  # protocol-level success...
        result = denied.json()["result"]
        # ...tool-level failure: fastmcp reports unknown tools as isError text.
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]


def test_mcp_health_route_unaffected(client):
    """Mounting an authenticated sub-app must not touch the open health probe."""
    assert client.get("/api/v1/health").status_code == 200


def test_tools_declare_the_read_only_contract(monkeypatch, client):
    """The security property must be machine-readable, not only documented.

    A host that cannot see `readOnlyHint` has to prompt for every call; one
    that can may auto-approve a surface that provably changes nothing.
    """
    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    with TestClient(app=client.app) as fresh:
        _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        listed = _post(
            fresh,
            "/mcp/",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            key="test-mcp-key",
        ).json()["result"]["tools"]

    assert listed
    for tool in listed:
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is True, tool["name"]
        assert annotations["destructiveHint"] is False, tool["name"]
        assert annotations["openWorldHint"] is False, tool["name"]
        assert annotations["title"], tool["name"]


def test_handler_failures_are_reported_as_errors(monkeypatch, client):
    """A 404 from a handler must reach the client as isError, not as an answer.

    The registry answers failures as data because the chat loop reads the dict;
    over MCP that shape is indistinguishable from a successful payload unless
    it is translated here.
    """
    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    with TestClient(app=client.app) as fresh:
        _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        called = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "explain_suggestion",
                    "arguments": {"suggestion_id": 10_000_000},
                },
            },
            key="test-mcp-key",
        ).json()["result"]

    assert called["isError"] is True
    # ...and the message survives, so the model can still say what went wrong.
    assert "not found" in called["content"][0]["text"]


def test_admin_only_tools_refuse_a_scoped_key(monkeypatch, client, db):
    """MCP resolves its own principal, so the admin line needs its own test.

    `call_tool` enforces it and the registry tests cover that, but this surface
    reaches the registry through `_principal_from_headers` rather than through
    the REST dependency those tests exercise.
    """
    from app.services.authorization import Principal

    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    monkeypatch.setattr(
        "app.mcp_server._principal_from_headers",
        lambda: Principal(is_admin=False, source="db", tenant_id=1),
    )
    with TestClient(app=client.app) as fresh:
        _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        called = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "get_evaluation_metrics", "arguments": {}},
            },
            key="test-mcp-key",
        ).json()["result"]

    assert called["isError"] is True
    assert "admin" in called["content"][0]["text"]


def test_a_call_authenticates_once_not_twice(monkeypatch, client, site):
    """The gate's work is carried down, not repeated inside the tool.

    ``ApiKeyGate`` resolves the key before JSON-RPC parsing so an
    unauthenticated caller cannot open a session. The tool then used to resolve
    the same header again against a second database session, which meant three
    sessions and two identical lookups per call and bought nothing.
    """
    from app import mcp_server

    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    real = mcp_server.authenticate_api_key
    calls: list[str | None] = []

    def counted(db, raw_key):
        calls.append(raw_key)
        return real(db, raw_key)

    monkeypatch.setattr(mcp_server, "authenticate_api_key", counted)

    with TestClient(app=client.app) as fresh:
        _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        calls.clear()
        response = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_sites", "arguments": {}},
            },
            key="test-mcp-key",
        )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False
    assert len(calls) == 1, f"authenticated {len(calls)} times for one tool call"


def test_an_unauthenticated_call_still_cannot_reach_a_tool(monkeypatch, client):
    """The carried principal must never become a way in.

    If the gate rejects, nothing downstream runs — so the fallback path inside
    the tool is unreachable for an unauthenticated caller, and the scope key it
    reads is never set by anyone but the gate.
    """
    monkeypatch.setattr(settings, "api_key", "right-key")
    with TestClient(app=client.app) as fresh:
        response = _post(
            fresh,
            "/mcp/",
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_sites", "arguments": {}},
            },
            key="wrong-key",
        )
    assert response.status_code == 401


def test_output_schemas_reach_the_client(monkeypatch, client):
    """A client must be able to read the result shape before it calls.

    `inputSchema` stops a model inventing an argument; `outputSchema` stops it
    inventing — or misreading — a field. It is declared for the tools where one
    number was being mistaken for another, and left off the rest.
    """
    monkeypatch.setattr(settings, "api_key", "test-mcp-key")
    with TestClient(app=client.app) as fresh:
        _post(fresh, "/mcp/", INITIALIZE, key="test-mcp-key")
        listed = _post(
            fresh,
            "/mcp/",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            key="test-mcp-key",
        ).json()["result"]["tools"]

    schemas = {tool["name"]: tool.get("outputSchema") for tool in listed}
    assert schemas["search_queue"] is not None
    assert schemas["get_queue_counts"] is None

    published = schemas["search_queue"]["properties"]
    assert "how many" in published["match_count"]["description"]
    assert "suggestions" in published

    # fastmcp inlines nested definitions, so the client gets one self-contained
    # schema — no $ref pointing at a $defs block that did not travel with it.
    page = published["page"]["properties"]
    assert "Never the answer to 'how many'" in page["returned"]["description"]
    assert "$ref" not in json.dumps(schemas["search_queue"])
    # And none of pydantic's generated titles, at any depth: a model quotes
    # them back as though they were content.
    assert "title" not in json.dumps(schemas["search_queue"])
