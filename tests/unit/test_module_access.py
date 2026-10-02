"""Module placement is admin-only even with a valid equipment claim."""
import pytest
from fastapi.testclient import TestClient

from opentrons_server.gateway.api import create_app

ADMIN = {"X-Edge-Key": "edge", "X-Auth-User": "admin@example.test", "X-Auth-Role": "admin"}
USER = {**ADMIN, "X-Auth-User": "user@example.test", "X-Auth-Role": "user"}
MODULE = {"module_name": "temperatureModuleV2", "serial_number": "synthetic-serial"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OT2_DECK_STATE_PATH", str(tmp_path / "deck.json"))
    monkeypatch.setenv("OT2_TIP_STATE_PATH", str(tmp_path / "tips.json"))
    monkeypatch.setenv("OT2_PLATE_STATE_PATH", str(tmp_path / "plate.json"))
    app = create_app(dry_run=True, auto_reconnect=False, edge_secret="edge", api_keys={"workflow": "key"})
    with TestClient(app) as client:
        result = client.post('/control/claim', json={"owner": "test", "session_id": "module-test", "ttl_s": 30})
        assert result.status_code == 200
        client.headers['X-Claim-Token'] = result.json()['claim_token']
        yield client


@pytest.mark.parametrize('headers', [USER, {}, {"X-Auth-User": "admin", "X-Auth-Role": "admin"},
    {"X-Edge-Key": "wrong", "X-Auth-User": "admin", "X-Auth-Role": "admin"},
    {"X-Edge-Key": "edge", "X-Auth-Role": "admin"},
    {"X-Api-Key": "key", "X-Auth-Role": "admin"}])
def test_module_assignment_requires_verified_admin(client, headers):
    r = client.post('/control/deck/declare', headers=headers, json={"slots": {"3": MODULE}})
    assert r.status_code == 403
    assert 'Only admins' in r.json()['detail']
    assert not client.app.state.service.decks.get()


def test_admin_assignment_move_and_remove(client):
    for slots in ({"3": MODULE}, {"4": MODULE}, {}):
        assert client.post('/control/deck/declare', headers=ADMIN, json={"slots": slots}).status_code == 200


def test_user_can_edit_plates_but_must_preserve_modules(client):
    assert client.post('/control/deck/declare', headers=ADMIN, json={"slots": {"3": MODULE}}).status_code == 200
    assert client.post('/control/deck/declare', headers=USER,
        json={"slots": {"3": MODULE, "2": "corning_96_wellplate_360ul_flat"}}).status_code == 200
    for slots in ({}, {"4": MODULE}, {"3": "corning_96_wellplate_360ul_flat"},
                  {"3": {**MODULE, "serial_number": "replacement"}}):
        assert client.post('/control/deck/declare', headers=USER, json={"slots": slots}).status_code == 403
    assert client.delete('/control/deck/declare', headers=USER).status_code == 403
    assert client.delete('/control/deck/declare', headers=ADMIN).status_code == 200


def test_setup_cannot_bypass_module_placement_role(client):
    setup = {"modules": [{"module_name": "temperatureModuleV2", "nickname": "temp", "location": "3"}]}
    assert client.post('/control/setup', headers=USER, json=setup).status_code == 403
    assert client.app.state.service.session_recipe['modules'] == []
    assert client.post('/control/setup', headers=ADMIN, json=setup).status_code == 200
    assert client.post('/control/setup', headers=USER, json=setup).status_code == 200
    assert client.post('/control/setup', headers=USER, json={}).status_code == 403
    assert client.post('/control/setup', headers=USER,
        json={"modules": [{**setup['modules'][0], "location": "4"}]}).status_code == 403


def test_chat_plan_approval_and_execution_require_admin(client):
    plan = client.post('/plans', json={"steps": [{"action": "deck.declare", "args": {"slots": {"3": MODULE}}}]}).json()
    path = '/plans/' + plan['plan_id']
    assert client.post(path+'/approve', headers=USER, json={"step_hash": plan['step_hash']}).status_code == 403
    assert client.post(path+'/approve', headers=ADMIN, json={"step_hash": plan['step_hash']}).status_code == 200
    assert client.post(path+'/execute', headers=USER).status_code == 403
    assert not client.app.state.service.decks.get()
    r = client.post(path+'/execute', headers=ADMIN)
    assert r.status_code == 200
    assert r.json()['status'] == 'executed'


def test_role_is_request_scoped_and_never_inferred_from_claim(client):
    assert client.get('/status', headers=ADMIN).json()['details']['permissions']['manage_modules'] is True
    assert client.get('/status', headers=USER).json()['details']['permissions']['manage_modules'] is False
    assert client.get('/status').json()['details']['permissions']['manage_modules'] is False
    assert client.post('/control/deck/declare', headers=ADMIN, json={"slots": {"3": MODULE}}).status_code == 200
    assert client.post('/control/deck/declare', json={"slots": {}}).status_code == 403
