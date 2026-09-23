"""下载 / 解包 / 校验 APK 所需的 Android arm64 运行时组件。

来源全部固定版本、固定 sha256：

* ``proot`` / ``loader``：Termux main 仓库；
* ``libtalloc`` / ``libandroid-shmem``：Termux main 仓库的依赖包；
* ``kdocs-cli`` 2.5.29 linux-arm64：复用 ``scripts/fetch_kdocs_cli.py``；
* Mozilla CA bundle：curl.se 官方 ``cacert.pem``；
* ``libxdgopen_shim.so``：本机 Android NDK 编译 ``xdgopen_shim.c``。

产物写入 ``android/app/src/main/jniLibs/arm64-v8a/``，并生成
``assets/runtime-manifest.json`` 记录每个二进制的 sha256 与来源，供 WpsRuntime
诊断页与 CI 校验。生成的二进制不入库，见 .gitignore。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "android" / "app" / "src" / "main" / "jniLibs" / "arm64-v8a"
DEFAULT_ASSETS = ROOT / "android" / "app" / "src" / "main" / "assets" / "runtime"
SHIM_SOURCE = ROOT / "android" / "app" / "src" / "main" / "cpp" / "xdgopen_shim.c"
KDOCS_VERSION = "2.5.29"
KDOCS_VENDOR = ROOT / "vendor" / "kdocs-cli"
TERMUX_BASE = "https://packages.termux.dev/apt/termux-main"
CA_URL = "https://curl.se/ca/cacert.pem"
# 2026-09-17 抓取的 curl.se 官方 Mozilla bundle；上游更新后需重新冻结。
CA_SHA256 = "f66dff1bdf8f96060b8177976f8b7d9254bc89bc4db933d769f7384d28480bc9"

# Termux 包在 apk 里的目标文件名。
PROOT_OUT = "libproot.so"
LOADER_OUT = "libproot_loader.so"
TALLOC_OUT = "libtalloc.so"
SHMEM_OUT = "libandroid-shmem.so"
KDOCS_OUT = "libkdocs_cli.so"
SHIM_OUT = "libxdgopen_shim.so"


@dataclass(frozen=True)
class TermuxPackage:
    name: str
    version: str
    filename: str
    sha256: str
    #: deb 内的源路径 -> jniLibs 目标文件名
    members: dict[str, str]
    #: Debian/Termux 仓库里的分组目录，例如 ``p/proot``。
    pool_group: str

    @property
    def url(self) -> str:
        return f"{TERMUX_BASE}/pool/main/{self.pool_group}/{self.filename}"


PACKAGES: tuple[TermuxPackage, ...] = (
    TermuxPackage(
        name="proot",
        # 2026-09-23：Termux 把 5.1.107.92 从 pool 移除，旧 pin 直接 404 卡住 APK 构建。
        # 按 dists/stable/main/binary-aarch64/Packages 升到 .94，并重新冻结官方 SHA256。
        version="5.1.107.94",
        filename="proot_5.1.107.94_aarch64.deb",
        sha256="b6fa26884d162f5234b0aba9f8a98971aad793706099464f7bd7eb1e21d63935",
        pool_group="p/proot",
        members={
            "data/data/com.termux/files/usr/bin/proot": PROOT_OUT,
            "data/data/com.termux/files/usr/libexec/proot/loader": LOADER_OUT,
        },
    ),
    TermuxPackage(
        name="libtalloc",
        version="2.4.3",
        filename="libtalloc_2.4.3_aarch64.deb",
        sha256="ac81ad623d74c209718b9f3acb2dd702cc8a88c431e820d212229910b4db29da",
        pool_group="libt/libtalloc",
        members={"data/data/com.termux/files/usr/lib/libtalloc.so.2.4.3": TALLOC_OUT},
    ),
    TermuxPackage(
        name="libandroid-shmem",
        version="0.7",
        filename="libandroid-shmem_0.7_aarch64.deb",
        sha256="0da3a24d558b93c92bcf8d611e0826a99ff96e396b148e6cdf33b47c47c57ff6",
        pool_group="liba/libandroid-shmem",
        members={"data/data/com.termux/files/usr/lib/libandroid-shmem.so": SHMEM_OUT},
    ),
)

REQUIRED_BINARIES = (PROOT_OUT, LOADER_OUT, TALLOC_OUT, SHMEM_OUT, KDOCS_OUT, SHIM_OUT)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_aarch64_elf(path: Path) -> None:
    """粗验 ELF64 / AArch64（e_machine=0xB7），防止把桌面 amd64 二进制装进 APK。"""
    with path.open("rb") as handle:
        header = handle.read(20)
    if (len(header) < 20 or header[:4] != b"\x7fELF" or header[4] != 2 or
            int.from_bytes(header[18:20], "little") != 0xB7):
        raise SystemExit(f"{path} 不是 AArch64 ELF，可先删除后重新运行本脚本")


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str, *, timeout: int = 120) -> bytes:
    print(f"  下载 {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "yikou-android-runtime/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise SystemExit(
                f"{url} 返回 404：Termux 轮转包版本时会把旧 .deb 从 pool 移除。"
                f"请在 {TERMUX_BASE}/dists/stable/main/binary-aarch64/Packages 里查"
                "新的 Filename 与 SHA256，更新 PACKAGES 后重跑本脚本。") from exc
        raise


def _read_ar_members(data: bytes) -> dict[str, bytes]:
    """读取 ``.deb`` 的 ar 容器，返回 member 名 -> 内容。不依赖 ``ar`` 命令。"""
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("不是合法的 ar 文件")
    members: dict[str, bytes] = {}
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as exc:
            raise ValueError(f"ar 头无法解析：{header!r}") from exc
        start = offset + 60
        end = start + size
        members[name] = data[start:end]
        offset = end + (size % 2)
    if not members:
        raise ValueError("ar 容器为空")
    return members


def deb_to_tar(data: bytes) -> tarfile.TarFile:
    """从 deb 字节流里找到 ``data.tar.*`` 并返回 tar（支持 xz/gz/bz2/zst）。"""
    members = _read_ar_members(data)
    for name in ("data.tar.xz", "data.tar.gz", "data.tar.bz2", "data.tar.zst", "data.tar"):
        payload = members.get(name)
        if payload is None:
            continue
        if name.endswith(".zst"):
            try:
                import zstandard  # type: ignore
            except ImportError as exc:  # pragma: no cover - Termux 包当前是 xz
                raise SystemExit("data.tar.zst 需要可选依赖 zstandard") from exc
            payload = zstandard.ZstdDecompressor().decompress(payload)
        return tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    raise ValueError(f"deb 内没有 data.tar.*：{sorted(members)}")


def _extract_member(deb: bytes, internal_path: str) -> bytes:
    with deb_to_tar(deb) as archive:
        member = None
        # tar member 名可能是 ``./path`` 或 ``path``，两者都接受。
        for candidate in ("./" + internal_path, internal_path):
            try:
                member = archive.getmember(candidate)
                break
            except KeyError:
                continue
        if member is None:
            raise ValueError(f"deb 内没有 {internal_path}")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError(f"deb 成员不是普通文件：{internal_path}")
        return handle.read()


def fetch_debs() -> dict[str, bytes]:
    """下载并校验三个 Termux 包，返回 package name -> deb 内容。"""
    result: dict[str, bytes] = {}
    for package in PACKAGES:
        data = _download(package.url)
        actual = sha256_of_bytes(data)
        if actual != package.sha256:
            raise SystemExit(
                f"{package.filename} sha256 校验失败：\n"
                f"  期望 {package.sha256}\n  实际 {actual}")
        result[package.name] = data
        print(f"  {package.filename} sha256 校验通过")
    return result


def _ensure_kdocs_cli(*, force: bool = False) -> Path:
    source = KDOCS_VENDOR / "kdocs-cli"
    if source.is_file() and not force:
        try:
            assert_aarch64_elf(source)
            return source
        except SystemExit as exc:
            print(f"  ⚠ 现有 kdocs-cli 不可用（{exc}），重新下载 linux-arm64…")
            force = True
    print("  vendor/kdocs-cli/ 缺少 linux-arm64 组件，调用现有的 fetch_kdocs_cli.py…")
    command = [sys.executable, str(ROOT / "scripts" / "fetch_kdocs_cli.py"),
               "--platform", "linux-arm64"]
    if force:
        command.append("--force")
    subprocess.run(command, check=True)
    if not source.is_file():
        raise SystemExit(f"下载后仍未找到 {source}")
    assert_aarch64_elf(source)
    return source


def _write_bytes(target: Path, data: bytes, *, mode: int = 0o755) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    try:
        target.chmod(mode)
    except OSError:
        pass


def _run_patchelf(*args: str) -> None:
    executable = shutil.which("patchelf")
    if executable is None:
        raise SystemExit(
            "缺少 patchelf。请安装后重试：apt-get install patchelf "
            "（macOS: brew install patchelf；Termux 上请用 proot/Ubuntu 环境执行构建）")
    subprocess.run([executable, *args], check=True)


def patch_elf(path: Path, *, replace_talloc: bool = False) -> None:
    """把 RUNPATH 改为 ``$ORIGIN``；需要时替换 Termux soname。"""
    _run_patchelf("--set-rpath", "$ORIGIN", str(path))
    if replace_talloc:
        # libproot 的 DT_NEEDED 原本是 libtalloc.so.2；APK 里统一放成 libtalloc.so。
        _run_patchelf("--replace-needed", "libtalloc.so.2", TALLOC_OUT, str(path))


def find_ndk() -> Path | None:
    for key in ("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "NDK_HOME"):
        value = os.environ.get(key, "").strip()
        if value and Path(value).is_dir():
            return Path(value)
    # GitHub Actions 的 setup-android 通常只设置 ANDROID_HOME / ANDROID_SDK_ROOT，
    # NDK 位于 <sdk>/ndk/<version>；取版本号最高的一个。
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(key, "").strip()
        if not sdk:
            continue
        ndk_root = Path(sdk) / "ndk"
        if not ndk_root.is_dir():
            continue
        versions = sorted((p for p in ndk_root.iterdir() if p.is_dir()),
                          key=lambda p: p.name, reverse=True)
        if versions:
            return versions[0]
    return None


def find_ndk_clang(ndk: Path) -> Path:
    """定位 arm64 / api26 的 clang 包装器（兼容各 NDK 版本目录命名）。"""
    prebuilt = ndk / "toolchains" / "llvm" / "prebuilt"
    candidates: list[Path] = []
    for host in sorted(prebuilt.iterdir(), reverse=True) if prebuilt.is_dir() else []:
        for api in ("26", "24", "21"):
            name = f"aarch64-linux-android{api}-clang"
            for candidate in (host / "bin" / name, host / "bin" / f"{name}.cmd"):
                if candidate.is_file():
                    candidates.append(candidate)
    if not candidates:
        raise SystemExit(
            f"在 NDK 里找不到 aarch64-linux-android*-clang：{ndk}")
    return candidates[0]


def build_xdgopen_shim_with_clang(compiler: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(compiler), "-O2", "-fPIE", "-pie", "-Wall", "-Wextra",
        "-o", str(target), str(SHIM_SOURCE),
    ]
    print(f"  编译 {SHIM_OUT}（{compiler}）")
    subprocess.run(command, check=True)
    try:
        target.chmod(0o755)
    except OSError:
        pass


def build_xdgopen_shim(ndk: Path, target: Path) -> None:
    build_xdgopen_shim_with_clang(find_ndk_clang(ndk), target)


def fetch_ca_bundle(target: Path, *, force: bool = False) -> None:
    if target.is_file() and not force:
        actual = sha256_of(target)
        if actual == CA_SHA256:
            print("  CA bundle 已就绪")
            return
    try:
        data = _download(CA_URL)
    except Exception as exc:  # noqa: BLE001
        if target.is_file():
            print(f"  ⚠ 联网获取 CA 失败，沿用本地 {target.name}：{exc}")
            return
        raise
    actual = sha256_of_bytes(data)
    if actual != CA_SHA256:
        raise SystemExit(
            f"CA bundle sha256 与冻结值不一致：\n  期望 {CA_SHA256}\n  实际 {actual}\n"
            "curl.se 的 Mozilla bundle 会随 CA 变更更新；请确认后更新本脚本常量。")
    _write_bytes(target, data, mode=0o644)
    print("  CA bundle 就绪")


def manifest_path(assets_dir: Path = DEFAULT_ASSETS) -> Path:
    return assets_dir / "runtime-manifest.json"


def write_manifest(out_dir: Path, assets_dir: Path) -> None:
    files: dict[str, dict[str, Any]] = {}
    for name in (*REQUIRED_BINARIES, "cacert.pem"):
        path = out_dir / name if (out_dir / name).is_file() else assets_dir / name
        if not path.is_file():
            continue
        files[name] = {"sha256": sha256_of(path), "size": path.stat().st_size}
    payload = {
        "kdocsCliVersion": KDOCS_VERSION,
        "target": "linux-arm64",
        "note": "由 scripts/fetch_android_runtime.py 生成，请勿手工修改",
        "files": files,
    }
    _write_bytes(manifest_path(assets_dir),
                 json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                 mode=0o644)


def check(out_dir: Path = DEFAULT_OUT, assets_dir: Path = DEFAULT_ASSETS) -> int:
    missing: list[str] = []
    for name in REQUIRED_BINARIES:
        if not (out_dir / name).is_file():
            missing.append(str(out_dir / name))
    ca = assets_dir / "cacert.pem"
    if not ca.is_file():
        missing.append(str(ca))
    manifest = manifest_path(assets_dir)
    if missing:
        print("❌ Android 运行时组件缺失：")
        for item in missing:
            print(f"  - {item}")
        print("   请运行：python scripts/fetch_android_runtime.py")
        return 1
    if not manifest.is_file():
        print("⚠ 运行时清单缺失（组件本身已就绪）：请重新运行一次获取脚本")
        return 1
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"❌ 运行时清单不可读：{exc}")
        return 1
    for name, info in (payload.get("files") or {}).items():
        path = out_dir / name if (out_dir / name).is_file() else assets_dir / name
        if not path.is_file() or sha256_of(path) != info.get("sha256"):
            print(f"❌ 运行时组件校验失败：{path}")
            return 1
    print(f"✅ Android arm64 运行时就绪（{out_dir}）")
    return 0


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="准备 APK 内置 kdocs-cli 运行时")
    parser.add_argument("--check", action="store_true", help="只校验本地是否就绪")
    parser.add_argument("--force", action="store_true", help="重新下载/覆盖组件")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--assets-dir", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--ndk", type=Path, default=None,
                        help="Android NDK 根目录；不传则读取 ANDROID_NDK_HOME")
    parser.add_argument("--no-shim", action="store_true",
                        help="跳过 xdg-open shim 编译（仅本机排障用）")
    parser.add_argument("--clang", type=Path, default=None,
                        help="直接用指定的 Android clang 编译 shim（Termux 本地测试）")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.check:
        return check(args.out_dir, args.assets_dir)

    out_dir = Path(args.out_dir).resolve()
    assets_dir = Path(args.assets_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    print("1/4 下载并校验 Termux 运行时包")
    debs = fetch_debs()
    for package in PACKAGES:
        data = debs[package.name]
        for internal, output in package.members.items():
            content = _extract_member(data, internal)
            _write_bytes(out_dir / output, content)
            print(f"  {output} <- {package.name} {package.version}")

    print("2/4 获取 kdocs-cli linux-arm64")
    kdocs = _ensure_kdocs_cli(force=args.force)
    _write_bytes(out_dir / KDOCS_OUT, kdocs.read_bytes())
    print(f"  {KDOCS_OUT} <- {kdocs}（{kdocs.stat().st_size} 字节）")

    print("3/4 patchelf 调整 RUNPATH / DT_NEEDED")
    patch_elf(out_dir / PROOT_OUT, replace_talloc=True)
    patch_elf(out_dir / TALLOC_OUT)
    patch_elf(out_dir / SHMEM_OUT)

    print("4/4 编译 xdg-open shim / CA bundle")
    ndk = Path(args.ndk).resolve() if args.ndk else find_ndk()
    if args.no_shim:
        print("  ⚠ 已跳过 xdg-open shim（--no-shim）")
    elif args.clang is not None:
        build_xdgopen_shim_with_clang(Path(args.clang).resolve(), out_dir / SHIM_OUT)
    elif ndk is None:
        raise SystemExit(
            "未找到 Android NDK。请设置 ANDROID_NDK_HOME，或给 --ndk/--clang 参数；"
            "仅做组件校验时可加 --check。")
    else:
        build_xdgopen_shim(ndk, out_dir / SHIM_OUT)
    if (out_dir / SHIM_OUT).is_file():
        patch_elf(out_dir / SHIM_OUT)
    fetch_ca_bundle(assets_dir / "cacert.pem", force=args.force)

    write_manifest(out_dir, assets_dir)
    print(f"✅ 完成：{out_dir}")
    return check(out_dir, assets_dir)


if __name__ == "__main__":
    raise SystemExit(main())
