"""T-205: `launchd_extra_path` reaches the LaunchAgent PATH; doctor warns on missing node/rg."""
from maestro import config as config_mod, fleet, health


def _stub(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / name
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)


def _plist(tmp_path, path):
    plist = tmp_path / "x.plist"
    plist.write_text(f"<dict><key>PATH</key><string>{path}</string></dict>", encoding="utf-8")
    return plist


def test_extra_path_in_plist(cfg, tmp_path):
    (cfg.home / "config.toml").write_text(
        f'[maestro]\nlaunchd_extra_path = ["{tmp_path}/volta/bin", "~/rgdir"]\n', encoding="utf-8")
    fresh = config_mod.load(str(cfg.home))
    args = dict(maestro_bin="/opt/m/bin/maestro", claude_bin="/opt/c/bin/claude")

    # Each call is a fresh regeneration; the dirs come from config, not hand edits.
    for _ in range(2):
        entries = fleet.launchd_path(fresh, **args).split(":")
        assert f"{tmp_path}/volta/bin" in entries
        assert str(__import__("pathlib").Path("~/rgdir").expanduser()) in entries
        assert entries[-4:] == ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    assert fleet.config_runner_dirs(fresh)[:1] == [f"{tmp_path}/volta/bin"]

    # Unset knob leaves the PATH byte-identical.
    assert fleet.launchd_path(cfg, **args) == (
        "/opt/m/bin:/opt/c/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")


def test_doctor_warns_missing_tools(cfg, tmp_path):
    bin_dir = tmp_path / "bin"
    _stub(bin_dir, "node")
    res = health.check_launchd_tools(cfg, 1000, plist=_plist(tmp_path, str(bin_dir)))
    assert res["status"] == "warn" and res["missing"] == ["rg"]
    assert "launchd_extra_path" in res["detail"]

    _stub(bin_dir, "rg")
    res = health.check_launchd_tools(cfg, 1000, plist=_plist(tmp_path, str(bin_dir)))
    assert res["status"] == "ok" and res["missing"] == []

    # Through the real report: the check is registered and no plist is silent.
    names = [c["name"] for c in health.run_checks(cfg, 1000, plist=_plist(tmp_path, str(bin_dir)))]
    assert "launchd_tools" in names
