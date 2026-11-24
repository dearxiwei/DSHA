package com.deepseekharness.app.util;

import static org.junit.Assert.*;
import org.junit.Test;

/**
 * Hermes 进程识别。
 *
 * <p>两个方向都要成立：认得出 App 自己起的 dashboard（否则停止/身份核验失效），并且绝不把用户自
 * 己跑的 python 进程当成它（历史上用 pkill 误杀过用户进程，这条不变式必须保持）。
 */
public class HermesProcSelectTest {
  private static final String DASH =
      "python3 -m hermes_cli.main dashboard --no-open --host 127.0.0.1 --port 9119";

  @Test
  public void recognisesDashboardProcess() {
    assertTrue(WebProcSel.looksLikeWeb(DASH));
    assertTrue(WebProcSel.looksLikeWeb("/opt/hermes/python/bin/python3 -m hermes_cli.main dashboard"));
    assertTrue(WebProcSel.looksLikeWeb("python -m hermes_cli.main dashboard --port 9119"));
  }

  @Test
  public void recognisesEntryScriptInvocation() {
    assertTrue(WebProcSel.looksLikeWeb("sh /opt/hermes/hermes.sh dashboard --no-open"));
  }

  @Test
  public void recognisesLaunchPreamble() {
    String command = WebLaunchCommand.build(false, "zh", "web", 9119, 7);
    assertTrue(WebProcSel.looksLikeWeb("bash\u0000-c\u0000" + command));
  }

  @Test
  public void refusesUserPythonAndOtherSubcommands() {
    assertFalse(WebProcSel.looksLikeWeb("python3 -m http.server 8080"));
    assertFalse(WebProcSel.looksLikeWeb("python3 -m hermes_cli.main chat"));
    assertFalse(WebProcSel.looksLikeWeb("python3 -m hermes_cli.main"));
    assertFalse(WebProcSel.looksLikeWeb("sh -c 'python3 -m http.server'"));
    assertFalse(WebProcSel.looksLikeWeb("sh /opt/hermes/hermes.sh status"));
    assertFalse(WebProcSel.looksLikeWeb("python3 manage.py runserver"));
  }

  @Test
  public void refusesContainerLaunchers() {
    assertFalse(WebProcSel.looksLikeWeb("proot --rootfs /data/rootfs /bin/sh -c python3"));
    assertFalse(WebProcSel.looksLikeWeb("libproroot-bridge.so --unknown"));
  }
}
