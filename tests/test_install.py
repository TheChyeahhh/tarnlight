"""tarnlight install / uninstall: they only make and remove the drop box; nothing else on the machine changes."""
import json
import pytest
from tarnlight import install as inst
from tarnlight.install import VAR, base_urls, install, uninstall


def run(fn, *a, **k):
    said = []; fn(*a, say=said.append, **k); return said


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A stand-in home folder; the real settings file and registry are never read for the checks below."""
    monkeypatch.setattr(inst, "SETTINGS", tmp_path / ".claude" / "settings.json")
    monkeypatch.setattr(inst.sys, "platform", "test")  # no Windows registry here
    monkeypatch.delenv(VAR, raising=False)
    return tmp_path


def test_install_makes_only_the_drop_box(home):
    inbox = home / ".tarnlight" / "inbox"
    said = run(install, inbox=inbox)
    assert inbox.is_dir() and sorted(p.name for p in home.iterdir()) == [".tarnlight"]  # nothing else touched
    assert any("never affects Jev" in s for s in said)
    assert any("already there" in s for s in run(install, inbox=inbox))


def test_uninstall_removes_it_and_says_what_was_unread(home):
    inbox = home / ".tarnlight" / "inbox"
    run(install, inbox=inbox); (inbox / "my-app-2026-09-26T19.jsonl").write_text("{}\n", encoding="utf-8")
    said = run(uninstall, inbox=inbox)
    assert not inbox.exists() and "with 1 file the console had not imported yet" in said[0]
    run(install, inbox=inbox); done = inbox / "my-app-2026-09-26T19.jsonl"; done.write_text("{}\n", encoding="utf-8")
    (inbox / ".positions.json").write_text(json.dumps({f"{done.name}:{done.stat().st_ino}": done.stat().st_size}), encoding="utf-8")
    assert run(uninstall, inbox=inbox)[0].endswith("inbox.")  # fully imported: nothing to warn about
    assert "Nothing to undo" in run(uninstall, inbox=inbox)[0]


def test_install_warns_when_something_still_points_calls_at_the_proxy(home):
    settings = inst.SETTINGS; settings.parent.mkdir(); settings.write_text(json.dumps({"env": {VAR: "http://127.0.0.1:7338"}}), encoding="utf-8")
    said = run(install, inbox=home / "inbox")
    assert any(str(settings) in s and "fail while Tarnlight is closed" in s for s in said)
    assert json.loads(settings.read_text(encoding="utf-8")) == {"env": {VAR: "http://127.0.0.1:7338"}}  # told, never changed


def test_base_urls_reads_every_place_it_can_be_set(home, monkeypatch):
    assert base_urls() == []
    monkeypatch.setenv(VAR, "http://127.0.0.1:7338/p/my-app")
    inst.SETTINGS.parent.mkdir(); inst.SETTINGS.write_bytes(b"\xef\xbb\xbf" + json.dumps({"env": {VAR: "https://api.typesafe.ai"}}).encode())
    assert [u for _, u in base_urls()] == ["http://127.0.0.1:7338/p/my-app", "https://api.typesafe.ai"]
    inst.SETTINGS.write_text("not json", encoding="utf-8")
    assert [u for _, u in base_urls()] == ["http://127.0.0.1:7338/p/my-app"]  # an unreadable file is skipped, not an error


def test_messages_have_no_em_dashes(home):
    inbox = home / "inbox"
    said = run(install, inbox=inbox) + run(uninstall, inbox=inbox)
    assert not any(chr(0x2014) in s or chr(0x2013) in s for s in said)  # em and en dashes


def test_the_cli_runs_install_and_uninstall(monkeypatch):
    from tarnlight import __main__ as cli
    calls = []
    monkeypatch.setattr(inst, "install", lambda **k: calls.append("install"))
    monkeypatch.setattr(inst, "uninstall", lambda **k: calls.append("uninstall"))
    assert cli.main(["install"]) == 0 and cli.main(["uninstall"]) == 0 and calls == ["install", "uninstall"]


def test_the_proxy_port_must_be_a_port(capsys, monkeypatch):
    from tarnlight import __main__ as cli
    monkeypatch.setattr(cli, "console", lambda *a: pytest.fail("a bad port reached the console"))  # never a real session
    for bad in ("70000", "0", "-1"):
        with pytest.raises(SystemExit): cli.main(["--proxy-port", bad])
    assert "not a TCP port" in capsys.readouterr().err
