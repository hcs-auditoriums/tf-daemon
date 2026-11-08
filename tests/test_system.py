import time

import pytest

from tf_daemon import system
from tf_daemon.app import build_modules, create_app
from tf_daemon.config import load_config


@pytest.fixture
def client_for(tmp_path):
    apps = []

    def make(config_path):
        cfg = load_config(None)
        app = create_app(cfg, build_modules(cfg, "synth", rf_source="off", qlxd_source="off"), config_path=config_path)
        apps.append(app)
        return app.test_client()

    yield make
    for app in apps:
        app.extensions["tf_stop"]()


def test_config_page_and_info(client_for, tmp_path):
    client = client_for(tmp_path / "config.toml")
    assert client.get("/config").status_code == 200
    info = client.get("/api/system/info").get_json()
    assert info["branch"] == "main" and info["remote"].endswith("tf-daemon.git")
    assert info["config_path"] == str((tmp_path / "config.toml").resolve())


def test_config_edit_validates_before_writing(client_for, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nport = 8080\n")
    client = client_for(path)
    assert client.get("/api/system/config").get_json()["text"] == "[server]\nport = 8080\n"

    for bad in ("[server\n", "[audio]\nsamplerate = 44100\n"):
        r = client.put("/api/system/config", json={"text": bad})
        assert r.status_code == 400 and r.get_json()["error"]
    assert client.put("/api/system/config", json={}).status_code == 400
    assert path.read_text() == "[server]\nport = 8080\n"  # untouched

    good = "[server]\nport = 9090\n"
    assert client.put("/api/system/config", json={"text": good}).get_json()["ok"]
    assert path.read_text() == good
    assert list(tmp_path.iterdir()) == [path]  # no temp files left behind


def test_config_editing_disabled_without_path(client_for):
    client = client_for(None)
    assert client.get("/api/system/config").status_code == 404
    assert client.put("/api/system/config", json={"text": ""}).status_code == 404


def test_check_reports_update(client_for, tmp_path, monkeypatch):
    monkeypatch.setattr(system, "_git", lambda *a, **k: "b" * 40 + "\trefs/heads/main")
    monkeypatch.setattr(system, "current_revision", lambda: "a" * 40)
    r = client_for(None).post("/api/system/check").get_json()
    assert r == {"ok": True, "current": "a" * 40, "latest": "b" * 40, "up_to_date": False}


@pytest.fixture
def update_files(tmp_path, monkeypatch):
    for name, attr in ((".update.json", "UPDATE_STATE"), (".update.log", "UPDATE_LOG"), (".update.env", "UPDATE_ENV")):
        monkeypatch.setattr(system, attr, tmp_path / name)
    monkeypatch.setattr(system, "_start_update_unit", lambda: False)  # exercise the in-process fallback
    return tmp_path


def _wait_final(client, timeout=5.0):
    deadline = time.time() + timeout
    while (s := client.get("/api/system/update").get_json())["running"] and time.time() < deadline:
        time.sleep(0.1)
    return s


def test_update_runs_one_at_a_time_and_reports_state(client_for, update_files, monkeypatch):
    script = update_files / "update.sh"
    # Stands in for deploy/update.sh: same env contract, same state file.
    script.write_text(
        'echo "remote=$UPDATE_REMOTE branch=$UPDATE_BRANCH"; sleep 1\n'
        'printf \'{"state": "up_to_date", "message": "already at abc", "pid": %d, "time": %d}\' $$ $(date +%s)'
        f' > {system.UPDATE_STATE}\n'
    )
    monkeypatch.setattr(system, "UPDATE_SCRIPT", script)
    client = client_for(None)
    assert client.get("/api/system/update").get_json()["state"] == "idle"

    assert client.post("/api/system/update").status_code == 202
    assert client.post("/api/system/update").status_code == 409
    assert client.get("/api/system/update").get_json()["running"]

    s = _wait_final(client)
    assert (s["state"], s["message"], s["running"]) == ("up_to_date", "already at abc", False)
    assert "remote=https://github.com/hcs-auditoriums/tf-daemon.git branch=main" in s["log"]
    assert "UPDATE_BRANCH=main" in system.UPDATE_ENV.read_text()


def test_update_that_dies_is_reported_failed(client_for, update_files, monkeypatch):
    script = update_files / "update.sh"
    script.write_text(f'printf \'{{"state": "running", "message": "x", "pid": %d, "time": 0}}\' $$ > {system.UPDATE_STATE}\n')
    monkeypatch.setattr(system, "UPDATE_SCRIPT", script)
    client = client_for(None)
    assert client.post("/api/system/update").status_code == 202
    s = _wait_final(client)
    assert s["state"] == "failed" and "stopped unexpectedly" in s["message"]


def test_restart_handoff_is_settled_by_the_new_process(update_files):
    system._write_state(state="restarting", message="restarting into bbb", pid=1, to="b" * 40)
    old = system.Updater("r", "main", running_revision="a" * 40)
    assert old.status()["state"] == "restarting"  # the old process is still the one running

    new = system.Updater("r", "main", running_revision="b" * 40)
    s = new.status()
    assert (s["state"], s["running"]) == ("done", False) and s["message"] == "updated to bbbbbbb"
    assert system._read_state()["state"] == "done"  # persisted

    system._write_state(state="restarting", message="", pid=1, to="b" * 40, time=0)
    s = system.Updater("r", "main", running_revision="a" * 40).status()
    assert s["state"] == "failed" and "not running the update" in s["message"]


def test_restart_answers_before_restarting(client_for, monkeypatch):
    calls = []
    monkeypatch.setattr(system, "RESTART_DELAY_S", 0.05)
    monkeypatch.setattr(system, "_restart_now", lambda: calls.append(time.time()))
    client = client_for(None)

    monkeypatch.setattr(system, "RESTART_CHECK_CMD", ["false"])  # sudo would refuse
    r = client.post("/api/system/restart")
    assert r.status_code == 500 and "install-service.sh" in r.get_json()["error"]

    monkeypatch.setattr(system, "RESTART_CHECK_CMD", ["true"])
    t0 = time.time()
    assert client.post("/api/system/restart").get_json() == {"ok": True}
    deadline = time.time() + 2
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1 and calls[0] >= t0  # restarted after answering, exactly once
