from __future__ import annotations

from app import config


def test_local_env_is_loaded_without_executing_or_overriding_environment(monkeypatch, tmp_path):
    (tmp_path / ".env.local").write_text(
        "# local only\nDEEPSEEK_API_KEY=local-test-key\nDEEPSEEK_MODEL=deepseek-flash\nGT_APP_MODE=production\nUNSUPPORTED_COMMAND=ignore\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "operator-choice")
    monkeypatch.delenv("GT_APP_MODE", raising=False)
    config._load_local_env()
    assert config.os.environ["DEEPSEEK_API_KEY"] == "local-test-key"
    assert config.os.environ["DEEPSEEK_MODEL"] == "operator-choice"
    assert config.os.environ["GT_APP_MODE"] == "production"
    assert "UNSUPPORTED_COMMAND" not in config.os.environ
