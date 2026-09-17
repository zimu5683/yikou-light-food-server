"""Tests for kdocs-cli 在 Termux/Android 上的运行环境适配。

kdocs-cli 是静态链接的 linux/arm64 Go 程序，看不到 Termux 对绝对路径的重写，
因此在 Android 上必然遇到「无 /etc/resolv.conf」和「无系统 CA 包」两个问题。
这里锁定 :func:`app.wps.sync.termux_cli_runtime` 的判定与产物。
"""
from __future__ import annotations

import os

import pytest

from app.wps.sync import KdocsCli, termux_cli_runtime
from app.wps import cli as wps_cli


@pytest.fixture()
def termux(tmp_path, monkeypatch):
    """伪造一个 Termux 前缀目录，并清掉宿主的 /etc/resolv.conf 判定。

    路径里必须真的包含 ``com.termux``：生产逻辑用它区分 Termux 与桌面，
    而不是把任意 PREFIX 都当 Termux。GitHub runner 的 /tmp 不含这个子串，
    因此旧测试只在 Termux 本机假绿。
    """
    prefix = tmp_path / "com.termux" / "usr"
    (prefix / "etc" / "tls").mkdir(parents=True)
    (prefix / "etc" / "tls" / "cert.pem").write_text("CA", encoding="utf-8")
    (prefix / "etc" / "resolv.conf").write_text("nameserver 8.8.8.8", encoding="utf-8")
    monkeypatch.setenv("PREFIX", str(prefix))
    monkeypatch.setattr(wps_cli.shutil, "which", lambda name: f"/fake/bin/{name}")
    return prefix


def _hide_system_resolv(monkeypatch, exists: bool):
    real_exists = wps_cli.Path.exists
    system_resolv = wps_cli.Path("/etc/resolv.conf")

    def fake_exists(self):
        if self == system_resolv:
            return exists
        return real_exists(self)

    monkeypatch.setattr(wps_cli.Path, "exists", fake_exists)


def _resolv_bind(termux) -> str:
    """Termux proot 的 resolv.conf bind 参数；Windows 上必须沿用 Path 的渲染格式。"""
    return f"{termux / 'etc' / 'resolv.conf'}:/etc/resolv.conf"


def test_non_termux_environment_is_untouched(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    # 桌面端必须完全不变：没有前缀、没有额外环境变量。
    assert termux_cli_runtime() == ([], {})


def test_prefix_without_termux_marker_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("PREFIX", str(tmp_path / "somewhere"))
    assert termux_cli_runtime() == ([], {})


def test_termux_binds_resolv_conf_and_points_ca_bundle(termux, monkeypatch):
    _hide_system_resolv(monkeypatch, exists=False)
    prefix, env = termux_cli_runtime()
    assert prefix[:3] == ["/fake/bin/proot", "-b", _resolv_bind(termux)]
    assert env["SSL_CERT_FILE"] == str(termux / "etc" / "tls" / "cert.pem")


def test_no_proot_needed_when_system_resolv_conf_exists(termux, monkeypatch):
    _hide_system_resolv(monkeypatch, exists=True)
    prefix, env = termux_cli_runtime()
    # 系统自带 resolv.conf 时不套 proot（避免无谓开销），但 CA 仍然要指过去。
    assert prefix == []
    assert env["SSL_CERT_FILE"]


def test_missing_proot_still_sets_ca_bundle(termux, monkeypatch):
    _hide_system_resolv(monkeypatch, exists=False)
    monkeypatch.setattr(wps_cli.shutil, "which", lambda name: None)
    prefix, env = termux_cli_runtime()
    assert prefix == []
    assert env["SSL_CERT_FILE"]


def test_login_argv_carries_the_proot_prefix(termux, monkeypatch):
    _hide_system_resolv(monkeypatch, exists=False)
    monkeypatch.setattr(wps_cli, "find_cli", lambda _=None: "/fake/kdocs-cli")
    cli = KdocsCli(None)
    argv = cli.login_argv()
    assert argv[:3] == ["/fake/bin/proot", "-b", _resolv_bind(termux)]
    assert argv[-2:] == ["auth", "login"]
    # auth login 需要联网轮询授权状态，所以也必须带上 CA 环境。
    assert cli.login_env()["SSL_CERT_FILE"]


def test_login_env_is_none_without_extra_vars(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(wps_cli, "find_cli", lambda _=None: "/fake/kdocs-cli")
    assert KdocsCli(None).login_env() is None


def test_run_once_passes_prefix_and_env_to_subprocess(termux, monkeypatch):
    """真正决定成败的一步：命令前缀与 SSL_CERT_FILE 必须落到 subprocess 上。"""
    _hide_system_resolv(monkeypatch, exists=False)
    monkeypatch.setattr(wps_cli, "find_cli", lambda _=None: "/fake/kdocs-cli")
    captured: dict[str, object] = {}

    class _Proc:
        stdout = '{"code": 0, "data": {}}'
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(wps_cli.subprocess, "run", fake_run)
    KdocsCli(None)._run_once("sheet", "get-sheets-info", params={"file_id": "x"})

    cmd = captured["cmd"]
    assert cmd[:3] == ["/fake/bin/proot", "-b", _resolv_bind(termux)]
    assert cmd[3] == "/fake/kdocs-cli"
    assert cmd[-2] == "--file"
    env = captured["env"]
    assert env["SSL_CERT_FILE"] == str(termux / "etc" / "tls" / "cert.pem")
    # 必须继承原环境（PATH 等），只是叠加了额外变量。
    assert env["PREFIX"] == str(termux)


def test_run_once_leaves_env_untouched_off_termux(monkeypatch):
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setattr(wps_cli, "find_cli", lambda _=None: "/fake/kdocs-cli")
    captured: dict[str, object] = {}

    class _Proc:
        stdout = '{"code": 0, "data": {}}'
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(wps_cli.subprocess, "run", fake_run)
    KdocsCli(None)._run_once("auth", "status")
    assert captured["cmd"] == ["/fake/kdocs-cli", "auth", "status"]
    # env=None 表示完全沿用父进程环境——桌面端行为不变的关键。
    assert captured["env"] is None


def test_extra_env_overrides_process_value(termux, monkeypatch):
    """进程里已有 SSL_CERT_FILE 时，Termux 的 CA 包必须覆盖它。"""
    _hide_system_resolv(monkeypatch, exists=False)
    monkeypatch.setenv("SSL_CERT_FILE", "/wrong/bundle.pem")
    _, env = termux_cli_runtime()
    assert env["SSL_CERT_FILE"] == str(termux / "etc" / "tls" / "cert.pem")
    assert os.environ["SSL_CERT_FILE"] == "/wrong/bundle.pem"

def test_find_cli_checks_repo_vendor_path(monkeypatch, tmp_path):
    """app/wps/cli.py 的 vendor 路径必须指向仓库根，而不是 app/vendor。"""
    from app.wps import cli as cli_mod

    monkeypatch.setattr(cli_mod, "__file__", str(tmp_path / "app" / "wps" / "cli.py"))
    monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
    monkeypatch.delenv("_MEIPASS", raising=False)
    monkeypatch.setattr(cli_mod.sys, "executable", str(tmp_path / "python"))

    expected = tmp_path / "vendor" / "kdocs-cli" / "kdocs-cli"
    real_is_file = cli_mod.Path.is_file

    def fake_is_file(self):
        return self == expected or real_is_file(self)

    monkeypatch.setattr(cli_mod.Path, "is_file", fake_is_file)
    assert cli_mod.find_cli() == str(expected)
