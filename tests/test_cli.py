import filematch.cli as cli


def test_cli_defaults_and_port(monkeypatch, capsys):
    calls = {}

    def fake_run(app, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    monkeypatch.delenv("FILEMATCH_HOST", raising=False)
    cli.main([])
    assert calls == {"host": "127.0.0.1", "port": 8765, "workers": 1}
    assert "http://127.0.0.1:8765/" in capsys.readouterr().out

    cli.main(["--port", "9001"])
    assert calls["port"] == 9001


def test_host_only_from_env(monkeypatch):
    calls = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.update(kw))
    monkeypatch.setenv("FILEMATCH_HOST", "0.0.0.0")
    cli.main([])
    assert calls["host"] == "0.0.0.0"
