import pytest
from fastapi.testclient import TestClient
from backend import store, media
from backend.app import app

@pytest.fixture(autouse=True)
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(store,'DATA',tmp_path/'data')
    monkeypatch.setattr(store,'SECRETS',tmp_path/'secrets')
    # Real pacing waits between search terms; tests assert behavior, not wall time.
    monkeypatch.setattr(media,'SEARCH_INTERVAL',0)
    for key in ('MOSS_API_KEY','LLM_API_KEY','LLM_BASE_URL','LLM_MODEL'): monkeypatch.delenv(key,raising=False)
    store.init()

@pytest.fixture
def client():
    with TestClient(app) as client:
        csrf=client.get('/api/session').json()['csrf']; client.headers['x-travel-csrf']=csrf
        yield client

@pytest.fixture
def task_id(client):
    return client.post('/api/tasks',json={'destination':'成都','days':2}).json()['id']
