import tracing


def test_disabled_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert not tracing.enabled() and tracing.callbacks() == []
    assert tracing.check_connection()["ok"] is False


def test_can_be_switched_off_even_with_keys(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test0000")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test0000")
    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    assert not tracing.enabled()


def test_mask_redacts_keys_recursively():
    data = {"a": "key sk-ant-abcdefghijklmnop1234 here", "b": ["Bearer abcdefghijklmnopqrstu", {"c": "pk-lf-12345678-aaaa"}], "n": 5}
    out = tracing.mask(data=data)
    assert "sk-ant" not in str(out) and "Bearer abc" not in str(out) and "pk-lf" not in str(out)
    assert out["n"] == 5 and out["a"].startswith("key [REDACTED]")
