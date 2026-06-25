import pytest

from agent_runtime.settings import get_settings


@pytest.fixture(autouse=True)
def disable_project_dotenv_for_tests(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_DISABLE_DOTENV", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
