"""Tests for the webhook route's event filtering / requires_analysis tagging."""
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

SECRET = "test-webhook-secret"

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    import importlib
    import app.core.config as config_mod
    importlib.reload(config_mod)
    import app.api.routes.webhooks as webhooks_mod
    importlib.reload(webhooks_mod)
    webhooks_mod.settings.GITHUB_WEBHOOK_SECRET = SECRET
    webhooks_mod._publisher.publish = AsyncMock(return_value="msg-id")

    import app.main as main_mod
    importlib.reload(main_mod)

    return TestClient(main_mod.app), webhooks_mod

def _sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"

def _workflow_run_payload(action: str, status: str, conclusion: str | None) -> dict:
    return {
        "action": action,
        "workflow_run": {
            "id": 12345,
            "workflow_id": 999,
            "name": "CI",
            "path": ".github/workflows/ci.yml",
            "head_branch": "main",
            "head_sha": "abc123",
            "status": status,
            "conclusion": conclusion,
            "run_started_at": "2026-06-18T00:00:00Z",
            "updated_at": "2026-06-18T00:05:00Z",
            "html_url": "https://github.com/acme/widgets/actions/runs/12345",
        },
        "repository": {"name": "widgets", "owner": {"login": "acme"}},
        "workflow": {"path": ".github/workflows/ci.yml"},
        "sender": {"login": "octocat"},
    }

def test_queued_event_is_published_without_analysis(client):
    test_client, webhooks_mod = client
    payload = _workflow_run_payload("requested", "queued", None)
    body = json.dumps(payload).encode()

    resp = test_client.post(
        "/webhooks/github",
        data=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "workflow_run",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"received": True, "published": True, "requires_analysis": False}
    webhooks_mod._publisher.publish.assert_awaited_once()
    published_message = webhooks_mod._publisher.publish.await_args.args[0]
    assert published_message["requires_analysis"] is False
    assert published_message["status"] == "queued"

def test_completed_success_is_published_without_analysis(client):
    test_client, webhooks_mod = client
    payload = _workflow_run_payload("completed", "completed", "success")
    body = json.dumps(payload).encode()

    resp = test_client.post(
        "/webhooks/github",
        data=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "workflow_run",
        },
    )
    assert resp.json()["requires_analysis"] is False

def test_completed_failure_requires_analysis(client):
    test_client, webhooks_mod = client
    payload = _workflow_run_payload("completed", "completed", "failure")
    body = json.dumps(payload).encode()

    resp = test_client.post(
        "/webhooks/github",
        data=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "workflow_run",
        },
    )
    assert resp.json() == {"received": True, "published": True, "requires_analysis": True}

def test_non_workflow_run_event_not_published(client):
    test_client, webhooks_mod = client
    body = json.dumps({"action": "opened"}).encode()

    resp = test_client.post(
        "/webhooks/github",
        data=body,
        headers={
            "X-Hub-Signature-256": _sign(body),
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.json() == {"received": True, "published": False}
    webhooks_mod._publisher.publish.assert_not_awaited()

def test_empty_webhook_secret_rejects_all_traffic(client, monkeypatch):
    test_client, webhooks_mod = client
    webhooks_mod.settings.GITHUB_WEBHOOK_SECRET = ""

    resp = test_client.post(
        "/webhooks/github",
        data=b"{}",
        headers={"X-GitHub-Event": "workflow_run"},
    )
    assert resp.status_code == 503
