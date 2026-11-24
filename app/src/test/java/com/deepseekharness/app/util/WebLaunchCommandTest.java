package com.deepseekharness.app.util;

import static org.junit.Assert.*;
import org.junit.Test;

public class WebLaunchCommandTest {
  @Test
  public void runsHermesDashboardWithFixedRuntimePaths() {
    String result = WebLaunchCommand.build(false, "zh", "web", 0, 42);
    assertTrue(result.startsWith("export PATH=\"" + HermesEntrypoint.NODE_BIN + ":"));
    assertTrue(result.contains("export HOME=/root && "));
    assertTrue(result.contains("export HERMES_HOME=" + HermesEntrypoint.HOME + " && "));
    assertTrue(result.contains("export HERMES_WEB_DIST=" + HermesEntrypoint.WEB_DIST + " && "));
    assertTrue(
        result.contains(
            "export PYTHONPATH=" + HermesEntrypoint.APP + "${PYTHONPATH:+:$PYTHONPATH} && "));
    assertTrue(result.contains("export DSHA_WEB_GENERATION=42"));
    assertTrue(
        result.endsWith(
            HermesEntrypoint.exec() + " dashboard --no-open --host 127.0.0.1 --port 0"));
    assertTrue(result.indexOf("echo $$ >") < result.indexOf("[ ! -e"));
    assertTrue(result.indexOf("[ ! -e") < result.indexOf(HermesEntrypoint.exec()));
    // 旧 dsh 注入必须彻底消失：否则会把 dsh 专属观察器塞进 Hermes 进程。
    assertFalse(result.contains("NODE_OPTIONS"));
    assertFalse(result.contains("startup-observer"));
    assertFalse(result.contains("DSH_HOME"));
    assertFalse(result.contains("dsh-bin"));
    assertFalse(result.contains("API_KEY"));
  }

  @Test
  public void keepsIdentityAndQuotesProfile() {
    String result = WebLaunchCommand.build(true, "en", "worker'; echo injected", 9119, 1);
    assertTrue(
        result.contains(
            "export DSHA_STARTUP_PROFILE=" + ShellQuote.arg("worker'; echo injected")));
    assertTrue(result.endsWith("--port 9119"));
    assertThrows(
        IllegalArgumentException.class, () -> WebLaunchCommand.build(true, "en", "web", -1, 1));
    assertThrows(
        IllegalArgumentException.class, () -> WebLaunchCommand.build(true, "en", "web", 65536, 1));
    assertThrows(
        IllegalArgumentException.class, () -> WebLaunchCommand.build(true, "en", "web", 1, -1));
  }
}
