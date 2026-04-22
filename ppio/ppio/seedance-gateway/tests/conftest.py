import sys
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def env_setup(monkeypatch):
    """Setup test environment variables."""
    monkeypatch.setenv("SEEDANCE_API_KEYS", "sk-test-key1")
    monkeypatch.setenv("SEEDANCE_BASE_URL", "https://api.ppio.com")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")  # Test DB
    monkeypatch.setenv("GATEWAY_ACCESS_TOKEN", "test-token-123")
    monkeypatch.setenv("TASK_POLL_INTERVAL", "1")
    monkeypatch.setenv("TASK_TIMEOUT", "60")


@pytest.fixture(autouse=True)
def reset_main_runtime_globals(monkeypatch):
    import main

    monkeypatch.setattr(main, "provider_store", None)
    monkeypatch.setattr(main, "seedance_client", None)
    monkeypatch.setattr(main, "task_manager", None)


@pytest.fixture
async def test_client(env_setup):
    """Create test client for FastAPI app."""
    from main import app
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client