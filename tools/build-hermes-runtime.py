#!/usr/bin/env python3
"""把锁定的 python / node / hermes 源码组装成离线覆盖层 hermes-runtime.bin。

设计要点（与 tools/build-dsh-runtime.py 保持同一输出契约）：
  * 确定性 gzip（mtime=0、PAX、level 9、路径排序），两次构建摘要必须一致；
  * 归档只含 posix 相对路径、只含 arm64 ELF，拒绝特殊文件与越界软链；
  * 产出 <output> 与 <output>.inputs.json（组件摘要 + 归档摘要 + 体积）；
  * 不使用 venv：pbs python 可重定位，依赖直接装进它的 site-packages，
    项目用 PYTHONPATH 提供，入口只走 `python3 -m hermes_cli.main`，
    因此归档里不会出现构建机的绝对路径。

本脚本不访问网络，除非显式要求下载（--prepare）或打印摘要（--print-digests）。
状态：草稿（M1A），尚未在 CI 与真机验证。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
LOCK_PATH = PROJECT / "tools/hermes-runtime/hermes.lock.json"
DEFAULT_OUTPUT = PROJECT / "app/src/main/assets/hermes-runtime.bin"
DEFAULT_STAGE = PROJECT / "app/build/hermes-runtime"
DEFAULT_CACHE = PROJECT / "app/build/hermes-runtime-cache"
CHUNK = 1 << 20
ELF_ARM64 = 183
# 这些字段为空时不允许构建：宁可停下，也不产出身份不明的归档。
REQUIRED = (
    ("hermes.commit", "hermes 提交号"),
    ("hermes.archiveSha256", "hermes 源码归档摘要"),
    ("python.url", "python 下载地址"),
    ("python.sha256", "python 归档摘要"),
    ("node.url", "node 下载地址"),
    ("node.sha256", "node 归档摘要"),
    ("buildTools.uv", "构建用 uv 版本"),
    ("buildTools.pnpm", "构建用 pnpm 版本"),
    ("webDist.cwd", "前端工程目录"),
    ("webDist.output", "前端产物目录"),
)


def load_lock(path: Path = LOCK_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def pick(lock: dict, dotted: str):
    node = lock
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def missing_fields(lock: dict) -> list[tuple[str, str]]:
    out = []
    for key, label in REQUIRED:
        value = pick(lock, key)
        if not isinstance(value, str) or not value.strip():
            out.append((key, label))
    return out


def require_complete(lock: dict) -> None:
    missing = missing_fields(lock)
    if missing:
        lines = ["HERMES_RUNTIME_LOCK_INCOMPLETE：以下字段必须固定后才能构建"]
        lines += [f"  - {key}（{label}）" for key, label in missing]
        lines.append("提示：python3 tools/build-hermes-runtime.py --print-digests")
        raise SystemExit("\n".join(lines))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, expected_sha: str, dest: Path) -> Path:
    if dest.is_file() and sha256_file(dest) == expected_sha:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".part")
    print(f"下载 {url}", file=sys.stderr)
    with urllib.request.urlopen(url) as response, temporary.open("wb") as out:
        shutil.copyfileobj(response, out, CHUNK)
    actual = sha256_file(temporary)
    if actual != expected_sha:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"ARCHIVE_DIGEST_MISMATCH：{dest.name} 得到 {actual}")
    temporary.replace(dest)
    return dest


def safe_extract(archive: Path, dest: Path, strip_components: int = 0) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        members = []
        for member in tar.getmembers():
            parts = [p for p in member.name.split("/") if p not in ("", ".")]
            if len(parts) <= strip_components:
                continue
            member.name = "/".join(parts[strip_components:])
            if any(part == ".." for part in parts):
                raise SystemExit(f"ARCHIVE_UNSAFE_PATH：{member.name}")
            if member.isdev() or member.isfifo():
                raise SystemExit(f"ARCHIVE_SPECIAL_FILE：{member.name}")
            members.append(member)
        try:
            tar.extractall(dest, members=members, filter="data")
        except TypeError:  # Python < 3.12 没有 filter 参数；上面的路径检查已覆盖越界情形
            tar.extractall(dest, members=members)


def prune(root: Path, patterns: list[str]) -> list[str]:
    """按 POSIX 相对路径删除不需要进包的目录/文件（只作用于 hermes 源码树）。"""
    from pathlib import PurePosixPath

    removed = []
    if not patterns:
        return removed
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if any(relative.match(pattern) for pattern in patterns):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            removed.append(relative.as_posix())
    return removed


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    printable = " ".join(cmd)
    print(f"$ {printable}" + (f"  (cwd={cwd})" if cwd else ""), file=sys.stderr)
    merged = dict(os.environ)
    merged.update(env or {})
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged, check=True)


def tree_digest(root: Path, excludes: list[str]) -> dict:
    """按排序后的相对路径 + 权限 + 内容计算组件摘要。"""
    import fnmatch

    digest = hashlib.sha256()
    files = 0
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in excludes):
            continue
        if path.is_dir():
            continue
        digest.update(relative.encode() + b"\0")
        if path.is_symlink():
            digest.update(b"L" + os.readlink(path).encode())
            continue
        digest.update(b"F" + oct(path.stat().st_mode & 0o777).encode() + b"\0")
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(CHUNK), b""):
                digest.update(block)
        files += 1
        total += path.stat().st_size
    return {"sha256": digest.hexdigest(), "files": files, "bytes": total}


def repo_pnpm_version(app: Path) -> str:
    """Hermes 用 pm/lock.json 钉死 pnpm 版本，仓库 preinstall 会逐字符核对。"""
    lock_file = app / "pm/lock.json"
    if not lock_file.is_file():
        return ""
    try:
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        version = data.get("packages", {}).get("pnpm", {}).get("version", "")
        return version if isinstance(version, str) else ""
    except (ValueError, OSError):
        return ""


def uv_overrides(pyproject: Path) -> list[str]:
    """读取 [tool.uv].override-dependencies；安装阶段必须与导出阶段用同一组，
    否则 uv pip 会按底层包元数据判定冲突（例如 browser-harness 钉 pillow 12.3.0）。"""
    import tomllib

    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    values = data.get("tool", {}).get("uv", {}).get("override-dependencies", [])
    return [str(item) for item in values if isinstance(item, str)]


def hermes_version(app: Path, commit: str) -> str:
    """Hermes 的 __version__ 是运行时从 git describe 推导的，离线源码树里没有静态版本。
    因此身份以提交号为准（前 12 位），与 inputs.json 的 hermesCommit 互相印证。"""
    version_file = app / "hermes_cli/__init__.py"
    if version_file.is_file():
        match = re.search(r"""^\s*__version__\s*=\s*["']([^"']+)["']""",
                          version_file.read_text(encoding="utf-8", errors="ignore"), re.M)
        if match:
            return match.group(1)
    return commit[:12] if commit else "unknown"


def write_entry_script(stage: Path, layout: dict) -> None:
    module = layout["entryModule"]
    script = (
        "#!/bin/sh\n"
        "# 由 tools/build-hermes-runtime.py 生成；不写入构建机路径。\n"
        "set -e\n"
        "R=/opt/hermes\n"
        'export PATH="$R/node/bin:$R/python/bin:$PATH"\n'
        'export PYTHONPATH="$R/app${PYTHONPATH:+:$PYTHONPATH}"\n'
        f'export HERMES_WEB_DIST="{layout["webDistEnv"]}"\n'
        f'exec "$R/python/bin/python3" -m {module} "$@"\n'
    )
    target = stage / layout["root"] / "hermes.sh"
    target.write_text(script, encoding="utf-8")
    target.chmod(0o755)


def stage_runtime(lock: dict, stage: Path, cache: Path, skip_verify: bool) -> dict:
    root = stage / lock["layout"]["root"]
    if stage.exists():
        shutil.rmtree(stage)
    root.mkdir(parents=True)

    python_tar = download(lock["python"]["url"], lock["python"]["sha256"], cache / "python.tar.gz")
    safe_extract(python_tar, root, lock["python"].get("stripComponents", 0))
    python_bin = root / "python/bin/python3"
    if not python_bin.is_file():
        raise SystemExit("PYTHON_LAYOUT_UNEXPECTED：解包后没有 python/bin/python3")

    node_tar = download(lock["node"]["url"], lock["node"]["sha256"], cache / "node.tar.xz")
    safe_extract(node_tar, root / "node", lock["node"].get("stripComponents", 1))
    if not (root / "node/bin/node").is_file():
        raise SystemExit("NODE_LAYOUT_UNEXPECTED：解包后没有 node/bin/node")

    hermes_tar = download(lock["hermes"]["archiveUrl"], lock["hermes"]["archiveSha256"], cache / "hermes.tar.gz")
    safe_extract(hermes_tar, root / "app", 1)
    app = root / "app"
    if not (app / "hermes_cli/main.py").is_file():
        raise SystemExit("HERMES_LAYOUT_UNEXPECTED：源码里没有 hermes_cli/main.py")

    requirements = cache / "requirements.txt"
    run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project",
         "--project", str(app), "-o", str(requirements)])
    # pyproject 里的 [tool.uv].override-dependencies 只对 uv 自己的解析器生效；
    # uv export 会按 override 钉住版本，但安装时 uv pip 仍会因底层包元数据冲突而拒绝，
    # 所以必须把同一组 override 显式传给 uv pip install。
    overrides = uv_overrides(app / "pyproject.toml")
    install = ["uv", "pip", "install", "--python", str(python_bin), "--no-cache",
               "--requirements", str(requirements)]
    if overrides:
        override_file = cache / "uv-overrides.txt"
        override_file.write_text("\n".join(overrides) + "\n", encoding="utf-8")
        install += ["--override", str(override_file)]
    python_env = {"UV_PYTHON_DOWNLOADS": "never", "UV_CACHE_DIR": str(cache / "uv")}
    run(install, env=python_env)

    env = {"PATH": f"{root / 'node/bin'}:{os.environ.get('PATH', '')}",
           "HERMES_WEB_DIST": str(root / "web_dist")}
    # 前端 engines 要求 node>=26；pnpm 版本由 hermes 自己的 pm/lock.json 钉死，
    # 仓库 preinstall 会逐字符核对，必须用它指定的版本而不是我们猜的版本。
    pnpm_version = repo_pnpm_version(app) or lock["buildTools"]["pnpm"]
    print(f"pnpm 版本取自 hermes pm/lock.json：{pnpm_version}", file=sys.stderr)
    # pnpm 必须装进随包 node 前缀（--prefix），否则会落到 runner 的全局前缀里。
    run([str(root / "node/bin/npm"), "install", "-g",
         f"pnpm@{pnpm_version}", "--prefix", str(root / "node"),
         "--no-fund", "--no-audit", "--allow-scripts=pnpm"], env=env)
    pnpm = root / "node/bin/pnpm"
    if not pnpm.exists():
        # npm 没建 bin 软链时，用随包 node 直接调 pnpm 的入口（路径保持相对，避免泄漏构建目录）。
        entry = root / "node/lib/node_modules/pnpm/bin/pnpm.cjs"
        if not entry.is_file():
            raise SystemExit("PNPM_INSTALL_FAILED：既没有 bin/pnpm，也没有 pnpm.cjs")
        pnpm.write_text('#!/bin/sh\nexec "$(dirname "$0")/node" '
                        '"$(dirname "$0")/../lib/node_modules/pnpm/bin/pnpm.cjs" "$@"\n',
                        encoding="utf-8")
        pnpm.chmod(0o755)
    command = lock["webDist"]["command"].replace("pnpm ", f"{pnpm} ", 1)
    run(["sh", "-c", command], cwd=app / lock["webDist"]["cwd"], env=env)
    web_dist = app / lock["webDist"]["cwd"] / lock["webDist"]["output"]
    if not (web_dist / "index.html").is_file():
        raise SystemExit(f"WEB_DIST_MISSING：{web_dist} 下没有 index.html")
    shutil.copytree(web_dist, root / "web_dist")

    # 前端构建需要 assets/ 与 scripts/，所以裁剪必须放在构建之后。
    pruned = prune(app, lock.get("excludes", []))
    if pruned:
        print(f"已从源码树剔除 {len(pruned)} 项（tests/website 等）", file=sys.stderr)

    version = hermes_version(app, lock["hermes"]["commit"])
    (root / "version").write_text(f"{version}\n{lock['hermes']['commit']}\n", encoding="utf-8")
    write_entry_script(stage, lock["layout"])

    if not skip_verify:
        run([str(python_bin), "-c",
             "import fastapi, uvicorn, ptyprocess, hermes_cli.main"], cwd=app,
            env={"PYTHONPATH": str(app), "HERMES_HOME": str(cache / "home")})

    return {"version": version, "app": app, "python": python_bin}


def assert_no_build_paths(stage: Path) -> None:
    """只检查会随搬运失效的文件：入口目录、shebang 脚本与 Python 路径文件。

    web_dist 里的 hermes-build.json、node_modules 下的 Makefile 会合法记录构建路径，
    它们是构建留痕而不是运行期依赖，不参与本检查。
    """
    needle = str(stage).encode()
    offenders = []
    for path in sorted(stage.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
            continue
        relative = path.relative_to(stage)
        posix = relative.as_posix()
        interesting = (
            relative.name.endswith((".pth", ".sh", ".egg-link", ".cfg"))
            or "/bin/" in "/" + posix
        )
        if not interesting:
            continue
        body = path.read_bytes()
        if needle in body:
            offenders.append(posix)
    if offenders:
        raise SystemExit("BUILD_PATH_LEAK：" + ", ".join(offenders[:10]))


def pack(stage: Path, output: Path, layout: dict) -> dict:
    root = stage / layout["root"]
    if not (root / "hermes.sh").is_file():
        raise SystemExit("STAGE_INCOMPLETE：缺少 hermes.sh")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    binaries, count, size = [], 0, 0
    try:
        with temporary.open("wb") as raw, gzip.GzipFile(
                fileobj=raw, mode="wb", filename="", mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(stage.rglob("*"), key=lambda item: item.relative_to(stage).as_posix()):
                    relative = path.relative_to(stage).as_posix()
                    if path.is_dir():
                        continue
                    name = relative
                    if path.is_symlink():
                        target = os.readlink(path)
                        if os.path.isabs(target):
                            raise SystemExit(f"ABSOLUTE_SYMLINK：{relative} -> {target}")
                        resolved = (path.parent / target).resolve()
                        resolved.relative_to(stage.resolve())
                        item = tarfile.TarInfo(name)
                        item.type = tarfile.SYMTYPE
                        item.linkname = target
                        item.mode = 0o777
                        archive.addfile(item)
                        continue
                    if not path.is_file():
                        raise SystemExit(f"SPECIAL_FILE：{relative}")
                    header = path.read_bytes()[:20]
                    executable = header.startswith((b"#!", b"\x7fELF"))
                    if header[:4] == b"\x7fELF":
                        if len(header) < 20 or int.from_bytes(header[18:20], "little") != ELF_ARM64:
                            raise SystemExit(f"NON_ARM64_ELF：{relative}")
                        binaries.append(relative)
                    item = tarfile.TarInfo(name)
                    item.size = path.stat().st_size
                    item.mode = 0o755 if executable else 0o644
                    item.mtime = 0
                    with path.open("rb") as stream:
                        archive.addfile(item, stream)
                    count += 1
                    size += item.size
                # 入口软链：usr/local/bin/hermes -> ../lib/hermes/hermes.sh
                link = layout["binLink"]
                item = tarfile.TarInfo(link)
                item.type = tarfile.SYMTYPE
                item.linkname = posixpath.relpath(layout["root"] + "/hermes.sh", posixpath.dirname(link))
                item.mode = 0o777
                archive.addfile(item)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"files": count, "unpacked_bytes": size,
            "archive_bytes": output.stat().st_size,
            "sha256": sha256_file(output), "arm64_binaries": len(binaries)}


def write_inputs(output: Path, lock: dict, components: dict, summary: dict) -> Path:
    payload = {
        "schema": 1,
        "runtime": "hermes",
        "version": components.get("version", "unknown"),
        "hermesCommit": lock["hermes"]["commit"],
        "python": lock["python"]["version"],
        "node": lock["node"]["version"],
        "components": components.get("digests", {}),
        "files": summary["files"],
        "unpacked_bytes": summary["unpacked_bytes"],
        "archive_bytes": summary["archive_bytes"],
        "archive_sha256": summary["sha256"],
        "arm64_binaries": summary["arm64_binaries"],
    }
    path = output.with_suffix(".inputs.json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def print_digests(lock: dict, cache: Path) -> None:
    targets = [
        ("hermes.archiveSha256", lock["hermes"]["archiveUrl"], "hermes.tar.gz"),
        ("python.sha256", lock["python"]["url"], "python.tar.gz"),
        ("node.sha256", lock["node"]["url"], "node.tar.xz"),
    ]
    for key, url, filename in targets:
        current = pick(lock, key)
        path = cache / filename
        if not (path.is_file() and current and sha256_file(path) == current):
            path.parent.mkdir(parents=True, exist_ok=True)
            print(f"下载 {url}", file=sys.stderr)
            with urllib.request.urlopen(url) as response, path.open("wb") as out:
                shutil.copyfileobj(response, out, CHUNK)
        print(f"{key} = {sha256_file(path)}")
    print("buildTools.uv / buildTools.pnpm 需要人工查版本后填入（不进运行时包）。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stage", type=Path, default=DEFAULT_STAGE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--pack-only", action="store_true", help="只打包已有 stage，不联网、不重装")
    parser.add_argument("--print-digests", action="store_true", help="下载并打印待补摘要")
    parser.add_argument("--check-lock", action="store_true", help="只检查锁文件是否完整")
    parser.add_argument("--skip-verify", action="store_true", help="跳过依赖导入自检（仅排查用）")
    args = parser.parse_args()

    lock = load_lock()
    # 统一绝对路径：打包与构建命令里出现相对路径会在切换 cwd 后失效。
    args.stage = args.stage.resolve()
    args.cache = args.cache.resolve()
    args.output = args.output.resolve()
    if args.check_lock:
        missing = missing_fields(lock)
        print("锁文件完整" if not missing else "\n".join(f"缺少 {k}（{v}）" for k, v in missing))
        return 0 if not missing else 1
    if args.print_digests:
        print_digests(lock, args.cache)
        return 0

    if not args.pack_only:
        require_complete(lock)

    if args.pack_only:
        if not pick(lock, "hermes.commit"):
            raise SystemExit("HERMES_RUNTIME_LOCK_INCOMPLETE：hermes.commit 至少要有值才能生成 inputs.json")
        components = {"version": "unknown", "digests": {}}
        if (lock["layout"]["root"]):
            version_file = args.stage / lock["layout"]["root"] / "version"
            if version_file.is_file():
                components["version"] = version_file.read_text(encoding="utf-8").splitlines()[0]
        assert_no_build_paths(args.stage)
        summary = pack(args.stage, args.output, lock["layout"])
        inputs = write_inputs(args.output, lock, components, summary)
        print(json.dumps({**summary, "inputs": str(inputs)}, indent=2, ensure_ascii=False))
        return 0

    staged = stage_runtime(lock, args.stage, args.cache, args.skip_verify)
    assert_no_build_paths(args.stage)
    root = args.stage / lock["layout"]["root"]
    digests = {
        name: tree_digest(root / name, lock.get("excludes", []))
        for name in ("python", "node", "app", "web_dist")
    }
    summary = pack(args.stage, args.output, lock["layout"])
    inputs = write_inputs(args.output, lock, {"version": staged["version"], "digests": digests}, summary)
    print(json.dumps({"version": staged["version"], "archive": str(args.output),
                      "inputs": str(inputs), "components": digests, **summary},
                     indent=2, ensure_ascii=False))
    print("提醒：产物仍需在 Linux arm64 真机（proot）冒烟一次，见计划书 M3。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
