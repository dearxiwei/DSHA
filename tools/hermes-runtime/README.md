> 状态：**草稿（M1A）**，未在 CI 验证；本目录只服务 Hermes Android，不参与现有 DSHA 构建。
> 目标产物：`app/src/main/assets/hermes-runtime.bin` + `app/src/main/assets/hermes-runtime.inputs.json`

# tools/hermes-runtime — Hermes 运行时包

## 布局（写入 rootfs 覆盖层）

```
opt/hermes/
├─ python/            python-build-standalone 3.14（可重定位，直接放行）
├─ node/              Node 24 LTS（Chat/TUI 与 pnpm 预构建用）
├─ app/               Hermes 源码（固定 tag/commit）
├─ web_dist/          预构建 dashboard 前端（由 CI 用 pnpm 产出）
├─ hermes.sh          入口脚本（无 stage 绝对路径，固定写 /opt/hermes）
└─ version            版本身份文件（供 Java 侧与打包核对）
usr/local/bin/hermes -> ../lib/hermes/hermes.sh
```

运行期由 App 侧设置：

```sh
HERMES_HOME=/root/.hermes
HERMES_WEB_DIST=/opt/hermes/web_dist
PYTHONPATH=/opt/hermes/app
PATH=/opt/hermes/node/bin:/opt/hermes/python/bin:...
```

入口命令（不走 venv console script，避免 shebang 里带构建机的绝对路径）：

```sh
exec /opt/hermes/python/bin/python3 -m hermes_cli.main dashboard --no-open --host 127.0.0.1 --port "$PORT"
```

## 为什么不用 venv

`python -m venv` 生成的 `bin/*` shebang 与 editable install 的 `.pth` 会写入**构建机绝对路径**，
打包进 APK 后全部失效。因此：

- 依赖直接装进 pbs python 自己的 `site-packages`（pbs 是 relocatable 构建）；
- 项目本身不安装，用 `PYTHONPATH=/opt/hermes/app` 提供；
- 入口只用 `python3 -m hermes_cli.main`，不依赖任何 console script。

## 使用

```sh
# 1) 首次：补全锁文件里空着的摘要与 commit（联网一次，结果粘回 hermes.lock.json）
python3 tools/build-hermes-runtime.py --print-digests

# 2) 准备 + 打包（可重复执行，幂等）
python3 tools/build-hermes-runtime.py --output app/src/main/assets/hermes-runtime.bin

# 3) 只打包已有 stage（离线复查）
python3 tools/build-hermes-runtime.py --pack-only --output /tmp/hermes-runtime.bin
```

构建机要求：Linux **aarch64**（与目标 rootfs 同为 glibc 2.39 / arm64），
`uv`、`pnpm`、`node`、`git`、`curl`。CI 上固定用 `ubuntu-24.04-arm`。

## 不可妥协的三条

1. **不可在 x86 上交叉产出**：依赖里含二进制 wheel 与 `.so`，交叉装的树会在真机 import 失败。
2. **禁止 relocatable 以外的绝对路径**：任何 shebang / `.pth` / `sysconfig data` 里出现构建机路径即视为失败。
3. **确定性打包**：`mtime=0`、`filename=''`、PAX、level 9、路径排序；两次构建摘要必须一致。
