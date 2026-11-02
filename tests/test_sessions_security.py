from fastapi.testclient import TestClient

from filematch.app import create_app
from filematch.config import Settings
from filematch.sessions import SessionStore
from tests.conftest import make_csv, session_of, upload

CSV = make_csv([["id", "name"], ["1", "Ann"]])


def test_session_cookie_flags(client):
    r = upload(client, file_a=("a.csv", CSV))
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("filematch_session=")
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie or "SameSite=Strict" in cookie
    assert len(client.cookies.get("filematch_session")) >= 40


def test_session_directory_created_with_prefix(client):
    upload(client, file_a=("a.csv", CSV))
    session = session_of(client)
    assert session.directory.name.startswith("filematch-")
    assert (session.directory / "file_a.csv").exists()


def test_clear_session_deletes_directory(client):
    upload(client, file_a=("a.csv", CSV))
    directory = session_of(client).directory
    assert directory.exists()
    r = client.post("/clear", follow_redirects=False)
    assert r.status_code == 303
    assert not directory.exists()
    assert len(client.app.state.store) == 0


def test_ttl_sweep_deletes_idle_session():
    now = [1000.0]
    store = SessionStore(ttl_seconds=3600, clock=lambda: now[0])
    idle = store.create()
    active = store.create()
    now[0] += 3000
    store.get(active.id)  # touch
    now[0] += 700  # idle is now 3700 s old, active 700 s
    assert store.sweep() == 1
    assert not idle.directory.exists()
    assert active.directory.exists()
    store.clear_all()
    assert not active.directory.exists()


def test_shutdown_deletes_all_sessions():
    app = create_app(Settings())
    with TestClient(app, base_url="http://localhost") as c:
        upload(c, file_a=("a.csv", CSV))
        directory = session_of(c).directory
        assert directory.exists()
    assert not directory.exists()


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["content-security-policy"] == "default-src 'self'"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert client.get("/static/app.js").headers["content-security-policy"] == "default-src 'self'"


def test_no_inline_scripts_or_styles(client):
    upload(client, file_a=("a.csv", CSV))
    page = client.get("/").text
    assert "<script>" not in page
    assert "style=" not in page


def test_trusted_hosts(settings):
    app = create_app(settings)
    for host, ok in [
        ("localhost", True),
        ("127.0.0.1:8765", True),
        ("[::1]:8765", True),
        ("evil.example.com", False),
        ("localhost.evil.example.com", False),
    ]:
        with TestClient(app, base_url=f"http://{host}") as c:
            assert (c.get("/").status_code == 200) is ok, host


def test_user_data_is_escaped(client):
    evil = make_csv([["<script>alert(1)</script>", "b"], ["1", "2"]])
    upload(client, file_a=("<img src=x onerror=alert(1)>.csv", evil))
    page = client.get("/").text
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;.csv" in page
    assert "<script>alert(1)" not in page


def test_templates_autoescape_enabled(client):
    env = client.app.state.templates.env
    for name in env.list_templates():
        template = env.get_template(name)
        assert env.autoescape is True, name
        assert template is not None


def test_no_openapi_docs(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_ai_preview_box_wraps_long_lines(client):
    css = client.get("/static/style.css").text
    rule = next(line for line in css.splitlines() if line.startswith("pre.payload"))
    assert "white-space: pre-wrap" in rule
