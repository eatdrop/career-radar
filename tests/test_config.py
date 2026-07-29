import pytest

from jobsearch_mcp_server.config import _env_bool


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_env_bool_accepts_explicit_true_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TEST_BOOLEAN", value)

    assert _env_bool("TEST_BOOLEAN", False) is True


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_env_bool_accepts_explicit_false_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("TEST_BOOLEAN", value)

    assert _env_bool("TEST_BOOLEAN", True) is False


def test_env_bool_rejects_ambiguous_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BOOLEAN", "truthy")

    with pytest.raises(ValueError, match="TEST_BOOLEAN"):
        _env_bool("TEST_BOOLEAN", False)
