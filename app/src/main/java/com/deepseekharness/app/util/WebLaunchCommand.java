package com.deepseekharness.app.util;

/**
 * Web 启动前置：固定运行树路径与环境变量，再 exec 入口脚本。
 *
 * <p>与旧 dsh 版本的差异：
 *
 * <ul>
 *   <li>不再注入 {@code NODE_OPTIONS/startup-observer.cjs}（Node 已不是入口进程）；
 *   <li>不再注入 {@code DSH_HOME/DSH_CONFIRM/DSH_PERMISSION_MODE}，Hermes 的审批与权限由它自己的
 *       config.yaml 管理；
 *   <li>保留哨兵文件、pid 写入与 generation：停止与身份核验依赖它们，且与运行时实现无关；
 *   <li>显式设置 {@code HERMES_WEB_DIST}，让 dashboard 使用预构建前端（真机不联网构建）。
 * </ul>
 */
public final class WebLaunchCommand {
  private WebLaunchCommand() {}

  public static String build(
      boolean confirmShell, String language, String profile, int listenPort, long generation) {
    if (listenPort < 0 || listenPort > 65535 || generation < 0)
      throw new IllegalArgumentException("WEB_LAUNCH_IDENTITY");
    // confirmShell 由 Hermes 自身的审批模式接管，保留参数只为稳定调用方签名。
    return "export PATH=\""
        + HermesEntrypoint.NODE_BIN
        + ":"
        + HermesEntrypoint.ROOT
        + "/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}\" && "
        + "export HOME=/root && "
        + "export HERMES_HOME="
        + HermesEntrypoint.HOME
        + " && "
        + "export HERMES_WEB_DIST="
        + HermesEntrypoint.WEB_DIST
        + " && "
        + "export PYTHONPATH="
        + HermesEntrypoint.APP
        + "${PYTHONPATH:+:$PYTHONPATH} && "
        + "export DSHA_UI_LANGUAGE="
        + ShellQuote.arg(language)
        + " && "
        + "export DSHA_STARTUP_PROFILE="
        + ShellQuote.arg(profile)
        + " && "
        + "export DSHA_WEB_GENERATION="
        + generation
        + " && cd /root && rm -f "
        + WebProcSel.IDENTITY_WEB
        + "; echo $$ > "
        + WebProcSel.PID_WEB
        + " 2>/dev/null; "
        + "[ ! -e "
        + WebProcSel.STOP_SENTINEL
        + " ] || exit 0; "
        + HermesEntrypoint.exec()
        + " dashboard --no-open --host 127.0.0.1 --port "
        + listenPort;
  }
}
