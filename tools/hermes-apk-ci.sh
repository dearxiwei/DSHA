#!/bin/sh
# hermes-apk-ci.sh —— 在「不能用 GitHub 官方 action」的 runner 上编译 DSHA / Hermes debug APK 的辅助脚本。
#
# 为什么要有这个脚本：本仓库 Actions 策略是 allowed_actions=local_only，workflow 里只要出现
# 官方 action 引用（`uses` 字段）就会整份 startup_failure。所以取源码、还原固定输入、装工具链、
# 上传产物全部改成普通命令，逻辑集中在这里；workflow 只负责按顺序调用子命令，本地也能照着跑一遍。
#
# 用法：bash tools/hermes-apk-ci.sh <子命令>
#   jdk        选出可用的 JDK 17（runner 目录名随镜像版本变化），导出 JAVA_HOME
#   inputs     按固定输入锁还原 app/src/main/assets 下的离线输入（含 SHA-256 校验）
#   sdk        按 ci/toolchain.lock.json 安装 platform / build-tools / NDK
#   qemu       让 x86_64 工具（Google aapt2、NDK llvm-strip）能在 arm64 宿主上执行
#   evidence   打印 java / sdkmanager / NDK / aapt2 版本证据
#   build      调用 build.sh 编译指定 Gradle 任务（默认 :app:assembleStandardDebug）
#   artifact   定位 APK，打印 sha256 与大小，并导出给后续步骤
#   summary    把构建身份写进 $GITHUB_STEP_SUMMARY
#   publish    用 gh release upload 上传产物（工作区没有 .git，必须显式 --repo）
#
# 说明：本脚本按 POSIX sh 写（dash 没有 pipefail），所以只用 `set -eu`；
# 需要管道的退出码时不用管道，或者把状态写进临时文件。workflow 的 run: 步骤仍然用 `set -euo pipefail`。
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

log() { printf '==> %s\n' "$*"; }
fail() { printf '::error::%s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,21p' "$0"
}

# ---------------------------------------------------------------- 通用探测

# runner 上 JAVA_HOME 的名字随镜像变（temurin-17 / java-17-openjdk-arm64 / ...），
# 判据是「真能跑 java 且主版本是 17」，不是目录存在。
jdk_home() {
    for candidate in "${JAVA_HOME:-}" "${ANDROID_JAVA_HOME:-}" \
        /usr/lib/jvm/temurin-17-jdk-arm64 /usr/lib/jvm/temurin-17-jdk \
        /usr/lib/jvm/java-17-openjdk-arm64 /usr/lib/jvm/java-17-openjdk-amd64 \
        /usr/lib/jvm/java-17-openjdk; do
        if [ -n "$candidate" ] && [ -x "$candidate/bin/java" ] \
            && "$candidate/bin/java" -version 2>&1 | grep -q 'version "17\.'; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    for candidate in /usr/lib/jvm/*; do
        if [ -x "$candidate/bin/java" ] \
            && "$candidate/bin/java" -version 2>&1 | grep -q 'version "17\.'; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# Android SDK 根目录：环境变量优先，其次 runner 镜像的默认位置。
sdk_root() {
    for candidate in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" \
        /usr/local/lib/android/sdk "$HOME/Android/Sdk" /opt/android-sdk; do
        if [ -n "$candidate" ] && [ -d "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

# 托管 runner 不一定把 sdkmanager 放进 PATH，从 SDK 根目录解析。
find_sdkmanager() {
    local sdk=$1 found=""
    if [ -x "$sdk/cmdline-tools/latest/bin/sdkmanager" ]; then
        printf '%s\n' "$sdk/cmdline-tools/latest/bin/sdkmanager"
        return 0
    fi
    found=$(find "$sdk/cmdline-tools" -maxdepth 4 -type f -name sdkmanager 2>/dev/null | sed -n '1p' || true)
    if [ -n "$found" ] && [ -x "$found" ]; then
        printf '%s\n' "$found"
        return 0
    fi
    found=$(command -v sdkmanager 2>/dev/null || true)
    if [ -n "$found" ]; then
        printf '%s\n' "$found"
        return 0
    fi
    return 1
}

export_env() {  # $1=键 $2=值：CI 里写 $GITHUB_ENV，本地只打印
    if [ -n "${GITHUB_ENV:-}" ]; then
        printf '%s=%s\n' "$1" "$2" >> "$GITHUB_ENV"
    else
        printf 'NOTE: 本地运行，不写 GITHUB_ENV：%s=%s\n' "$1" "$2"
    fi
}

# ---------------------------------------------------------------- jdk

cmd_jdk() {
    local home
    home=$(jdk_home || true)
    if [ -z "$home" ]; then
        printf '::error::找不到 JDK 17：已试 $JAVA_HOME、$ANDROID_JAVA_HOME、/usr/lib/jvm/*\n' >&2
        ls -1 /usr/lib/jvm 2>/dev/null || true
        printf 'runner 需要预装 JDK 17；本脚本不下载 JDK（避免引入未固定的二进制）。\n' >&2
        exit 1
    fi
    export JAVA_HOME="$home"
    export_env JAVA_HOME "$JAVA_HOME"
    log "JAVA_HOME=$JAVA_HOME"
    "$JAVA_HOME/bin/java" -version 2>&1
}

# ---------------------------------------------------------------- inputs

# 固定输入：ci/sunset-inputs.lock.json 记录的 url + sha256，交给 tools/ci-assets.py 下载、
# 校验 ZIP 摘要、按源码白名单挑出成员、再逐文件核对 runtime-descriptor 里的 pin，最后原子替换。
# 落点固定为 app/src/main/assets/<name> 与 tools/recovery-runtime/archives/<asset>。
cmd_inputs() {
    local url sha fmt name path
    url="${HERMES_INPUT_URL:-}"
    sha="${HERMES_INPUT_SHA:-}"
    fmt="${HERMES_INPUT_FORMAT:-bundle}"
    if [ -z "$url" ] || [ -z "$sha" ]; then
        if [ ! -f ci/sunset-inputs.lock.json ]; then
            fail "缺少 ci/sunset-inputs.lock.json，无法解析固定输入"
        fi
        url=$(python3 -c 'import json;print(json.load(open("ci/sunset-inputs.lock.json",encoding="utf-8"))["url"])')
        sha=$(python3 -c 'import json;print(json.load(open("ci/sunset-inputs.lock.json",encoding="utf-8"))["sha256"])')
        fmt=$(python3 -c 'import json;print(json.load(open("ci/sunset-inputs.lock.json",encoding="utf-8"))["format"])')
    fi
    log "固定输入来源：$url"
    log "期望 SHA-256：$sha（格式 $fmt）"
    set -- --url "$url" --sha256 "$sha"
    if [ "$fmt" = source-archive ]; then
        set -- "$@" --source-archive
    fi
    # ci-assets.py 自己会核对 ZIP 摘要与每个成员的源码 pin；失败就是不还原，不降级。
    python3 tools/ci-assets.py "$@"
    for name in offline-rootfs.bin dsh-runtime.bin dsh-runtime.inputs.json \
        ubuntu-tools.bin ubuntu-tools.inputs.json pnpm-runtime.bin \
        python-support.bin glibc-python.tar.gz adb-wheels.tar.gz gh-cli.tar.gz; do
        path="app/src/main/assets/$name"
        if [ ! -f "$path" ]; then
            fail "固定输入缺失：$path"
        fi
        printf '    %-24s %12s bytes  %s\n' "$name" "$(wc -c < "$path" | tr -d ' ')" \
            "$(sha256sum "$path" | cut -d' ' -f1)"
    done
}

# ---------------------------------------------------------------- sdk

install_packages() {  # $@ = sdkmanager 包名；失败重试一次，最终失败打印日志尾部
    local attempt=1 log_file="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hermes-sdkmanager.log"
    log "sdkmanager 安装：$*"
    while :; do
        if timeout 900 "$SDKMANAGER" --sdk_root="$SDK" "$@" >> "$log_file" 2>&1; then
            return 0
        fi
        printf '::warning::sdkmanager 第 %s 次失败或超时（日志 %s）\n' "$attempt" "$log_file" >&2
        tail -n 40 "$log_file" >&2 || true
        if [ "$attempt" -ge 2 ]; then
            printf '::error::sdkmanager 失败（已尝试 %s 次）：%s\n' "$attempt" "$*" >&2
            return 1
        fi
        attempt=$((attempt + 1))
        sleep 5
    done
}

cmd_sdk() {
    local platform buildtools ndk

    SDK=$(sdk_root || true)
    if [ -z "$SDK" ]; then
        # arm64 镜像不一定预装 Android SDK。这里不做「下载任意网址」的兜底：
        # 需要显式给出 cmdline-tools 的 url + sha256，两条都齐才装。
        if [ -n "${HERMES_CMDLINE_TOOLS_URL:-}" ] && [ -n "${HERMES_CMDLINE_TOOLS_SHA256:-}" ]; then
            log "runner 没有 Android SDK，按 HERMES_CMDLINE_TOOLS_URL 安装 cmdline-tools"
            local zip="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/cmdline-tools.zip"
            SDK="$HOME/android-sdk"
            mkdir -p "$SDK/cmdline-tools"
            curl -fsSL -o "$zip" "$HERMES_CMDLINE_TOOLS_URL"
            echo "$HERMES_CMDLINE_TOOLS_SHA256  $zip" | sha256sum -c - >/dev/null \
                || fail "cmdline-tools 摘要不符（期望 $HERMES_CMDLINE_TOOLS_SHA256）"
            rm -rf "$SDK/cmdline-tools/latest"
            ( cd "$SDK/cmdline-tools" && unzip -q "$zip" && mv cmdline-tools latest )
        else
            printf '::error::runner 上没有 Android SDK（$ANDROID_HOME/$ANDROID_SDK_ROOT 都不存在）\n' >&2
            printf '二选一：换带 SDK 的镜像；或在 workflow 里给出 HERMES_CMDLINE_TOOLS_URL/SHA256 由本脚本安装。\n' >&2
            exit 1
        fi
    fi
    export ANDROID_HOME="$SDK" ANDROID_SDK_ROOT="$SDK"
    export_env ANDROID_HOME "$SDK"
    export_env ANDROID_SDK_ROOT "$SDK"
    log "ANDROID_SDK=$SDK"

    SDKMANAGER=$(find_sdkmanager "$SDK" || true)
    if [ -z "$SDKMANAGER" ]; then
        fail "在 $SDK 里找不到 cmdline-tools/sdkmanager，无法安装 platform / build-tools / NDK"
    fi
    log "SDKMANAGER=$SDKMANAGER"

    # 版本只在源码锁上取，不在脚本里另写一份。
    platform=$(python3 tools/ci-toolchain.py --field androidPlatform)
    buildtools=$(python3 tools/ci-toolchain.py --field buildTools)
    ndk=$(python3 tools/ci-toolchain.py --field ndk)

    # 协议先接受：sdkmanager 没接受过 license 时会交互等待，超时表现像「卡住」。
    yes | "$SDKMANAGER" --sdk_root="$SDK" --licenses > /dev/null 2>&1 || true

    if [ -f "$SDK/platforms/$platform/android.jar" ]; then
        log "platforms;$platform 已存在，跳过"
    else
        if ! install_packages "platforms;$platform"; then
            # 少数 cmdline-tools 版本只认 android-37；退一步再试，仍然失败就报错。
            log "platforms;$platform 安装失败，改试 platforms;android-37"
            install_packages "platforms;android-37" \
                || fail "platform 安装失败：platforms;$platform / platforms;android-37"
        fi
    fi
    if [ -x "$SDK/build-tools/$buildtools/aapt2" ]; then
        log "build-tools;$buildtools 已存在，跳过"
    else
        install_packages "build-tools;$buildtools" \
            || fail "build-tools 安装失败：build-tools;$buildtools（AGP 9.1.1 要求 >= 36.0.0）"
    fi
    if grep -qs "^Pkg.Revision *= *$ndk" "$SDK/ndk/$ndk/source.properties"; then
        log "ndk;$ndk 已存在，跳过"
    else
        install_packages "ndk;$ndk" || fail "NDK 安装失败：ndk;$ndk（app/build.gradle 的 ndkVersion）"
    fi

    # 装完按实际文件复核，半截安装不能当成可用环境交给 Gradle。
    [ -f "$SDK/platforms/$platform/android.jar" ] \
        || fail "安装后仍缺 $SDK/platforms/$platform/android.jar"
    [ -x "$SDK/build-tools/$buildtools/aapt2" ] \
        || fail "安装后仍缺 $SDK/build-tools/$buildtools/aapt2"
    grep -qs "^Pkg.Revision *= *$ndk" "$SDK/ndk/$ndk/source.properties" \
        || fail "安装后 NDK 版本不符：期望 $ndk"
}

# ---------------------------------------------------------------- qemu

# arm64 宿主上必须解决的事：Google 仓库发布的 Linux aapt2 与 NDK 的 llvm-strip 都是 x86_64 ELF。
# 本项目在 aarch64 本地构建时也是用 qemu 跑 aapt2（见 docs/design/onboarding-20261031/implementation.md）。
# 这里在 CI 上装 qemu-user-static 并注册 binfmt_misc；注册不成功就直接失败，不让 Gradle 报
# 一个看不懂的 “Exec format error”。
cmd_qemu() {
    local machine aapt2
    machine=$(uname -m)
    if [ "$machine" = x86_64 ] || [ "$machine" = amd64 ]; then
        log "宿主是 $machine，不需要 x86_64 兼容层"
        return 0
    fi
    SDK=$(sdk_root || true)
    [ -n "$SDK" ] || fail "找不到 Android SDK，先运行 sdk 子命令"
    aapt2="$SDK/build-tools/$(python3 tools/ci-toolchain.py --field buildTools)/aapt2"
    [ -x "$aapt2" ] || fail "找不到 aapt2：$aapt2（先运行 sdk 子命令）"

    if "$aapt2" version > /dev/null 2>&1; then
        log "aapt2 已可直接执行（宿主的 binfmt_misc 已注册 x86_64）"
        return 0
    fi
    log "aapt2 无法直接执行，安装 qemu-user-static 并注册 binfmt_misc"
    file "$aapt2" || true
    export DEBIAN_FRONTEND=noninteractive
    sudo apt-get update -qq
    sudo apt-get install -y -qq qemu-user-static binfmt-support || true
    # 两种注册方式都试：update-binfmts（qemu-user-static 自带的 /usr/share/binfmts 条目）
    # 与 systemd-binfmt（/usr/lib/binfmt.d/*.conf）。失败不立即退出，下面用实际执行判定。
    sudo update-binfmts --enable qemu-x86_64 > /dev/null 2>&1 || true
    sudo systemctl restart systemd-binfmt > /dev/null 2>&1 || true

    if "$aapt2" version > /dev/null 2>&1; then
        log "x86_64 二进制已可执行（qemu-user 生效）"
        return 0
    fi
    printf '::error::装完 qemu-user-static 仍无法执行 x86_64 的 aapt2\n' >&2
    ls -1 /proc/sys/fs/binfmt_misc/ 2>/dev/null || printf 'binfmt_misc 未挂载\n' >&2
    ls -1 /usr/libexec/qemu-binfmt/ 2>/dev/null || true
    "$aapt2" version 2>&1 | sed -n '1,5p' >&2 || true
    exit 1
}

# ---------------------------------------------------------------- evidence

cmd_evidence() {
    local platform buildtools ndk sdkmanager
    SDK=$(sdk_root || true)
    [ -n "$SDK" ] || fail "找不到 Android SDK"
    JAVA_HOME=$(jdk_home || true)
    [ -n "$JAVA_HOME" ] || fail "找不到 JDK 17"
    sdkmanager=$(find_sdkmanager "$SDK" || true)
    platform=$(python3 tools/ci-toolchain.py --field androidPlatform)
    buildtools=$(python3 tools/ci-toolchain.py --field buildTools)
    ndk=$(python3 tools/ci-toolchain.py --field ndk)

    log "宿主：$(uname -s) $(uname -m)，nproc=$(nproc 2>/dev/null || echo '?')"
    free -h 2>/dev/null | sed -n '1,2p' || true
    df -h . | sed -n '1,2p' || true
    printf -- '--- java ---\n'
    "$JAVA_HOME/bin/java" -version 2>&1
    printf -- '--- python ---\n'
    python3 --version 2>&1
    printf -- '--- sdkmanager 已装组件（过滤后）---\n'
    if [ -n "$sdkmanager" ]; then
        # 只是证据：sdkmanager 查询失败不影响后面的编译。
        "$sdkmanager" --sdk_root="$SDK" --list_installed 2>/dev/null \
            | grep -E '^[[:space:]]*(platforms|build-tools|ndk);' || true
    else
        printf 'NOTE: 找不到 sdkmanager，跳过\n'
    fi
    printf -- '--- 实际文件证据 ---\n'
    printf 'platform  : %s\n' "$SDK/platforms/$platform/android.jar"
    printf 'build-tools: %s\n' "$SDK/build-tools/$buildtools"
    printf 'aapt2     : %s\n' "$("$SDK/build-tools/$buildtools/aapt2" version 2>&1 | sed -n '1p')"
    printf 'NDK       : %s\n' "$SDK/ndk/$ndk"
    grep -E '^Pkg.Revision' "$SDK/ndk/$ndk/source.properties" 2>/dev/null || true
    printf -- '--- gradle wrapper ---\n'
    grep -E '^distributionUrl' gradle/wrapper/gradle-wrapper.properties || true
}

# ---------------------------------------------------------------- build

# 托管 runner 的出口 IP 会被 Maven Central 限流（实测 429 Too Many Requests），
# 而 settings.gradle 用 FAIL_ON_PROJECT_REPOS，所以只能改 settings 级仓库：
# 把 repo.maven.apache.org 换成 Google 的 Central 镜像，保留 google()/jitpack 等既有仓库。
maven_init_script() {  # $1=输出路径
    cat > "$1" <<'GROOVY'
def mirror = System.getenv('HERMES_MAVEN_MIRROR') ?: 'https://maven-central.storage-download.googleapis.com/maven2/'
settingsEvaluated { settings ->
    def retarget = { handler ->
        def central = handler.findAll { repo ->
            repo instanceof org.gradle.api.artifacts.repositories.MavenArtifactRepository &&
                (repo.url.toString().contains('repo.maven.apache.org') ||
                 repo.url.toString().contains('repo1.maven.org'))
        }
        central.each { handler.remove(it) }
        handler.maven { url = mirror }
    }
    retarget(settings.pluginManagement.repositories)
    retarget(settings.dependencyResolutionManagement.repositories)
}
allprojects {
    buildscript.repositories.all { repo ->
        if (repo instanceof org.gradle.api.artifacts.repositories.MavenArtifactRepository &&
            repo.url.toString().contains('repo.maven.apache.org')) {
            repo.url = mirror
        }
    }
}
GROOVY
}

cmd_build() {
    local task status log_file status_file init
    task="${1:-${HERMES_TASK:-:app:assembleStandardDebug}}"
    [ -n "$task" ] || task=:app:assembleStandardDebug
    log_file="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hermes-apk-build.log"
    status_file="$log_file.status"
    init="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hermes-maven-mirror.gradle"
    maven_init_script "$init"
    log "构建 $task（Maven 走镜像，完整日志 $log_file）"
    # 同时「流式输出到 CI 日志」和「保留真实退出码」：POSIX sh 没有 pipefail，
    # 所以让写日志的管道里先记下 build.sh 的状态码，再从文件读回来。
    { bash build.sh -I "$init" "$task" 2>&1; printf '%s\n' "$?" > "$status_file"; } | tee "$log_file"
    status=$(cat "$status_file" 2>/dev/null || echo 1)
    if [ "$status" -ne 0 ]; then
        printf '::error::构建失败（退出码 %s），日志尾部：\n' "$status" >&2
        tail -n 60 "$log_file" >&2 || true
        exit "$status"
    fi
    log "构建成功"
}

# ---------------------------------------------------------------- artifact

cmd_artifact() {
    local dir apk bytes sha
    dir="app/build/outputs/apk/standard/debug"
    apk=""
    for candidate in "$dir/app-standard-debug.apk" "$dir"/*.apk; do
        if [ -f "$candidate" ]; then
            apk="$candidate"
            break
        fi
    done
    if [ -z "$apk" ]; then
        printf '::error::没有找到 debug APK（目录 %s）\n' "$dir" >&2
        ls -l app/build/outputs/apk 2>/dev/null >&2 || true
        exit 1
    fi
    bytes=$(wc -c < "$apk" | tr -d ' ')
    sha=$(sha256sum "$apk" | cut -d' ' -f1)
    printf 'APK       : %s\n' "$apk"
    printf '大小      : %s bytes\n' "$bytes"
    printf 'SHA-256   : %s\n' "$sha"
    export_env HERMES_APK "$apk"
    export_env HERMES_APK_SHA256 "$sha"
    export_env HERMES_APK_BYTES "$bytes"
}

# ---------------------------------------------------------------- summary

cmd_summary() {
    local target apk bytes sha mb java_line
    apk="${HERMES_APK:-}"
    if [ -z "$apk" ] || [ ! -f "$apk" ]; then
        fail "缺少 HERMES_APK（先运行 artifact 子命令）"
    fi
    bytes="${HERMES_APK_BYTES:-$(wc -c < "$apk" | tr -d ' ')}"
    sha="${HERMES_APK_SHA256:-$(sha256sum "$apk" | cut -d' ' -f1)}"
    mb=$(awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1048576 }')
    java_line="n/a"
    if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
        java_line=$("$JAVA_HOME/bin/java" -version 2>&1 | sed -n '1p')
    fi
    target="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
    {
        printf '## DSHA / Hermes debug APK\n\n'
        printf '| 项 | 值 |\n| --- | --- |\n'
        printf '| commit | `%s` |\n' "${GITHUB_SHA:-local}"
        printf '| task | `%s` |\n' "${HERMES_TASK:-:app:assembleStandardDebug}"
        printf '| runner | `%s` |\n' "$(uname -m)"
        printf '| release tag | `%s` |\n' "${HERMES_RELEASE_TAG:-}"
        printf '| APK | `%s` |\n' "$(basename "$apk")"
        printf '| 大小 | %s bytes（%s MiB） |\n' "$bytes" "$mb"
        printf '| SHA-256 | `%s` |\n' "$sha"
        printf '| 签名 | debug keystore（非发布证书，仅用于验证编译） |\n'
        printf '\n### 工具链\n\n```\n'
        printf 'java: %s\n' "$java_line"
        printf 'androidPlatform: %s\n' "$(python3 tools/ci-toolchain.py --field androidPlatform)"
        printf 'buildTools: %s\n' "$(python3 tools/ci-toolchain.py --field buildTools)"
        printf 'ndk: %s\n' "$(python3 tools/ci-toolchain.py --field ndk)"
        printf 'sdk: %s\n' "${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
        printf '```\n'
    } >> "$target"
    log "摘要已写入 $target"
}

# ---------------------------------------------------------------- publish

cmd_publish() {
    local tag repo apk short name out_dir
    tag="${1:-${HERMES_RELEASE_TAG:-hermes-apk-debug}}"
    repo="${GITHUB_REPOSITORY:-${HERMES_REPO:-}}"
    apk="${HERMES_APK:-}"
    [ -n "$repo" ] || fail "缺少仓库名（GITHUB_REPOSITORY 或 HERMES_REPO）"
    [ -n "${GH_TOKEN:-}" ] || fail "缺少 GH_TOKEN，无法上传 Release"
    [ -n "$apk" ] && [ -f "$apk" ] || fail "缺少 HERMES_APK（先运行 artifact 子命令）"
    short=$(printf '%s' "${GITHUB_SHA:-local}" | cut -c1-12)
    name="hermes-apk-$short-standard-debug.apk"
    out_dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hermes-apk-release"
    mkdir -p "$out_dir"
    cp "$apk" "$out_dir/$name"
    ( cd "$out_dir" && sha256sum "$name" > "$name.sha256" )

    # 源码是 tarball 解出来的，工作区没有 .git：gh 每条命令都必须显式 --repo。
    if ! gh release view "$tag" --repo "$repo" > /dev/null 2>&1; then
        log "Release $tag 不存在，创建"
        gh release create "$tag" --repo "$repo" \
            --title "hermes-apk $tag" \
            --notes "DSHA / Hermes debug APK（由 hermes-apk workflow 生成，debug keystore 签名，仅用于验证编译）" \
            || fail "创建 Release $tag 失败（需要 contents: write）"
    fi
    log "上传 $name 到 $repo@$tag"
    gh release upload "$tag" --repo "$repo" "$out_dir/$name" "$out_dir/$name.sha256" --clobber \
        || fail "gh release upload 失败"
    gh release view "$tag" --repo "$repo" --json assets \
        --jq '.assets[] | "\(.name) \(.size) bytes"' 2>/dev/null || true
}

# ---------------------------------------------------------------- 入口

case "${1:-}" in
    jdk) cmd_jdk ;;
    inputs) cmd_inputs ;;
    sdk) cmd_sdk ;;
    qemu) cmd_qemu ;;
    evidence) cmd_evidence ;;
    build) shift; cmd_build "${1:-}" ;;
    artifact) cmd_artifact ;;
    summary) cmd_summary ;;
    publish) shift; cmd_publish "${1:-}" ;;
    *) usage; exit 2 ;;
esac
