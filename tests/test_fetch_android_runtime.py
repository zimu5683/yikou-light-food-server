"""``scripts/fetch_android_runtime.py`` 的校验逻辑回归锁。

不在单测里联网：下载逻辑只测 URL 拼接与 sha 校验分支；deb 解包用内存里合成的
ar + tar.gz；``check``/``write_manifest`` 用临时文件验证篡改必被发现。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "fetch_android_runtime.py"


def _load():
    spec = importlib.util.spec_from_file_location("fetch_android_runtime", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _ar_member(name: str, data: bytes) -> bytes:
    name_bytes = (name + "/")[:16].ljust(16).encode("ascii")
    header = name_bytes + b"0".ljust(12) + b"0".ljust(6) + b"0".ljust(6)
    header += b"100644".ljust(8) + str(len(data)).encode("ascii").ljust(10) + b"`\n"
    assert len(header) == 60
    return header + data + (b"\n" if len(data) % 2 else b"")


def test_deb_tar_extracts_member_from_synthetic_package():
    module = _load()
    payload = b"proot-binary"
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:gz") as tf:
        info = tarfile.TarInfo("data/data/com.termux/files/usr/bin/proot")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    deb = (b"!<arch>\n"
           + _ar_member("debian-binary", b"2.0\n")
           + _ar_member("control.tar.xz", b"ignored")
           + _ar_member("data.tar.gz", tar_buffer.getvalue()))

    assert module._extract_member(
        deb, "data/data/com.termux/files/usr/bin/proot") == payload


def test_aarch64_elf_guard_rejects_wrong_machine(tmp_path):
    module = _load()
    good = tmp_path / "good.so"
    good.write_bytes(b"\x7fELF" + b"\x02" + b"\x00" * 13 + (0xB7).to_bytes(2, "little"))
    module.assert_aarch64_elf(good)  # 不抛异常

    bad = tmp_path / "bad.so"
    bad.write_bytes(b"\x7fELF" + b"\x02" + b"\x00" * 13 + (0x3E).to_bytes(2, "little"))
    try:
        module.assert_aarch64_elf(bad)
    except SystemExit:
        pass
    else:  # pragma: no cover
        raise AssertionError("amd64 ELF 必须被拒绝")


def test_ar_parser_rejects_non_ar_data():
    module = _load()
    try:
        module._read_ar_members(b"not-an-ar")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("非 ar 数据必须报错")


def test_package_urls_match_termux_repository_paths():
    module = _load()
    expected = {
        "proot": ("p/proot", "proot_5.1.107.94_aarch64.deb"),
        "libtalloc": ("libt/libtalloc", "libtalloc_2.4.3_aarch64.deb"),
        "libandroid-shmem": ("liba/libandroid-shmem", "libandroid-shmem_0.7_aarch64.deb"),
    }
    for package in module.PACKAGES:
        pool, filename = expected[package.name]
        assert package.pool_group == pool
        assert package.filename == filename
        assert package.url == (
            f"https://packages.termux.dev/apt/termux-main/pool/main/{pool}/{filename}")


def _populate_runtime(module, out_dir: Path, assets_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)
    for name in module.REQUIRED_BINARIES:
        (out_dir / name).write_bytes(f"fake-{name}".encode())
    (assets_dir / "cacert.pem").write_bytes(b"fake-ca")
    module.write_manifest(out_dir, assets_dir)


def test_fetch_kdocs_cli_supports_forced_linux_arm64():
    """APK 在 x86_64 CI runner 上必须能明确下载 linux-arm64 组件。"""
    kdocs_path = SCRIPT.parent / "fetch_kdocs_cli.py"
    spec = importlib.util.spec_from_file_location("fetch_kdocs_cli_test", kdocs_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    assert module.resolve("linux-arm64") == ("linux-arm64", "tar.gz", "kdocs-cli")


def test_check_accepts_then_rejects_tampered_binary(tmp_path):
    module = _load()
    out_dir = tmp_path / "jniLibs"
    assets_dir = tmp_path / "assets"
    _populate_runtime(module, out_dir, assets_dir)

    assert module.check(out_dir, assets_dir) == 0

    # 篡改任意一个二进制后必须被 sha256 发现，避免“文件在但内容坏了”的假绿。
    (out_dir / module.PROOT_OUT).write_bytes(b"tampered")
    assert module.check(out_dir, assets_dir) == 1


def test_write_manifest_records_all_required_components_and_ca(tmp_path):
    module = _load()
    out_dir = tmp_path / "jniLibs"
    assets_dir = tmp_path / "assets"
    _populate_runtime(module, out_dir, assets_dir)

    payload = json.loads((assets_dir / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert payload["kdocsCliVersion"] == module.KDOCS_VERSION
    assert set(payload["files"]) == {*module.REQUIRED_BINARIES, "cacert.pem"}
    for name, info in payload["files"].items():
        path = out_dir / name if (out_dir / name).is_file() else assets_dir / name
        assert info["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
