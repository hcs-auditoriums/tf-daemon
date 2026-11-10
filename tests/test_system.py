import json
import time

import pytest
from helpers import make_config, wait_until

from tf_daemon import system
from tf_daemon.app import build_modules, create_app


@pytest.fixture
def client_for(tmp_path):
    apps = []

    def make(config_path):
        cfg = make_config()
        app = create_app(cfg, build_modules(cfg), config_path=config_path)
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


def test_settings_edit_the_file_one_line_at_a_time(client_for, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# mine\n[tf5]\neq = true\neq_write = false   # ask first\n\n[server]\nport = 8080\n")
    client = client_for(path)
    got = client.get("/api/system/config").get_json()
    tables = {t["name"]: {s["key"]: s for s in t["settings"]} for t in got["settings"]}
    eq_write = tables["tf5"]["tf5.eq_write"]
    assert eq_write["value"] is False and eq_write["set"] and eq_write["kind"] == "bool" and eq_write["help"]
    assert tables["tf5"]["tf5.host"]["set"] is False and tables["rf"]["rf.source"]["choices"] == [
        "device",
        "demo",
        "off",
    ]
    assert "tf5.eq_write" in got["common"]

    def put(key, value):
        return client.put("/api/system/setting", json={"key": key, "value": value})

    r = put("tf5.eq_write", True).get_json()
    assert r["ok"] and "eq_write = true   # ask first\n" in path.read_text()  # in place, comment kept
    put("tf5.fader_write", True)  # missing: added under [tf5]
    put("rf.auto_connect", False)  # no [rf]: the table added at the end
    put("tf5.host", "10.0.0.5")  # text, quoted
    put("qlxd.hosts", "10.0.0.7, 10.0.0.8:2202")  # a list typed with commas
    put("server.port", "9090")  # a number typed in a box
    assert path.read_text() == (
        '# mine\n[tf5]\nhost = "10.0.0.5"\nfader_write = true\neq = true\neq_write = true   # ask first\n\n'
        "[server]\nport = 9090\n\n[rf]\nauto_connect = false\n\n"
        '[qlxd]\nhosts = ["10.0.0.7", "10.0.0.8:2202"]\n'
    )
    assert put("server.port", None).get_json()["ok"]  # back to the default: the line goes
    assert "port" not in path.read_text()

    # What the config can't take is refused, the file left as it was.
    before = path.read_text()
    r = put("tf5.eq_write", False)  # fader_write needs it
    assert r.status_code == 400 and "fader_write" in r.get_json()["error"]
    assert put("server.port", "eighty").status_code == 400
    assert put("server.nope", 1).status_code == 400
    assert put("dsp.fft_size", 1.5).status_code == 400
    assert path.read_text() == before


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
    state = lambda: client.get("/api/system/update").get_json()  # noqa: E731
    wait_until(lambda: not state()["running"], timeout=timeout)
    return state()


@pytest.mark.slow
def test_update_runs_one_at_a_time_and_reports_state(client_for, update_files, monkeypatch):
    script = update_files / "update.sh"
    # Stands in for deploy/update.sh: same env contract, same state file.
    script.write_text(
        'echo "remote=$UPDATE_REMOTE branch=$UPDATE_BRANCH"; sleep 1\n'
        'printf \'{"state": "up_to_date", "message": "already at abc", "pid": %d, "time": %d}\' $$ $(date +%s)'
        f" > {system.UPDATE_STATE}\n"
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


def test_update_to_a_branch_or_commit(client_for, update_files, monkeypatch):
    script = update_files / "update.sh"
    script.write_text(
        'echo "ref=$UPDATE_REF"\n'
        'printf \'{"state": "up_to_date", "message": "x", "pid": %d, "time": %d}\' $$ $(date +%s)'
        f" > {system.UPDATE_STATE}\n"
    )
    monkeypatch.setattr(system, "UPDATE_SCRIPT", script)
    client = client_for(None)
    for bad in ("-x", "a..b", "with space", "x\nUPDATE_REMOTE=evil", 5):
        assert client.post("/api/system/update", json={"ref": bad}).status_code == 400, bad
    assert client.post("/api/system/update", json={"ref": "feature/try-this"}).status_code == 202
    assert "ref=feature/try-this" in _wait_final(client)["log"]
    assert "UPDATE_REF=feature/try-this" in system.UPDATE_ENV.read_text()
    assert client.post("/api/system/update", json={"ref": "1d84fec"}).status_code == 202
    _wait_final(client)
    assert client.post("/api/system/update").status_code == 202  # none: the configured branch
    assert "ref=main" in _wait_final(client)["log"]


def test_info_says_which_branch_or_commit_runs(client_for, update_files, monkeypatch):
    monkeypatch.setattr(system, "current_revision", lambda: "c" * 40)
    system.UPDATE_STATE.write_text(json.dumps({"state": "done", "to": "c" * 40, "ref": "feature/x", "time": 0}))
    assert client_for(None).get("/api/system/info").get_json()["ref"] == "feature/x"
    system.UPDATE_STATE.write_text(json.dumps({"state": "done", "to": "d" * 40, "ref": "feature/x", "time": 0}))
    assert client_for(None).get("/api/system/info").get_json()["ref"] is None  # not what runs now


def test_refs_lists_the_remote_branches(client_for, monkeypatch):
    out = "a" * 40 + "\trefs/heads/feature/x\n" + "b" * 40 + "\trefs/heads/main"
    monkeypatch.setattr(system, "_git", lambda *a, **k: out)
    assert client_for(None).get("/api/system/refs").get_json()["branches"] == ["main", "feature/x"]  # its own first


def test_update_that_dies_is_reported_failed(client_for, update_files, monkeypatch):
    script = update_files / "update.sh"
    state = '{"state": "running", "message": "x", "pid": %d, "time": 0}'
    script.write_text(f"printf '{state}' $$ > {system.UPDATE_STATE}\n")
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
    wait_until(lambda: calls, timeout=2)
    assert len(calls) == 1 and calls[0] >= t0  # restarted after answering, exactly once
