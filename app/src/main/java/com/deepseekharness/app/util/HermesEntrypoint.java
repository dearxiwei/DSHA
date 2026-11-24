package com.deepseekharness.app.util;

/**
 * Hermes 运行时入口常量。
 *
 * <p>运行树是构建期产出的离线覆盖层（见 tools/hermes-runtime/README.md），固定挂在 {@link #ROOT}，
 * 因此这里只暴露运行期约定，不在 App 侧拼装 python/venv 路径：
 *
 * <ul>
 *   <li>{@link #SCRIPT}：入口脚本，内部 exec 随包 python 的 {@code -m hermes_cli.main}；
 *   <li>{@link #PYTHON}：随包解释器，供诊断与就绪失败排查使用；
 *   <li>{@link #HOME}：{@code HERMES_HOME}，会话、技能与密钥都在这里；
 *   <li>{@link #WEB_DIST}：预构建 dashboard 前端，避免真机首启触发 pnpm 构建。
 * </ul>
 */
public final class HermesEntrypoint {
  public static final String ROOT = "/opt/hermes";
  public static final String SCRIPT = ROOT + "/hermes.sh";
  public static final String PYTHON = ROOT + "/python/bin/python3";
  public static final String APP = ROOT + "/app";
  public static final String WEB_DIST = ROOT + "/web_dist";
  public static final String HOME = "/root/.hermes";
  public static final String NODE_BIN = ROOT + "/node/bin";

  private HermesEntrypoint() {}

  /** 启动前置里使用的 exec 片段；子命令由调用方拼接。 */
  public static String exec() {
    return "exec " + SCRIPT;
  }
}
