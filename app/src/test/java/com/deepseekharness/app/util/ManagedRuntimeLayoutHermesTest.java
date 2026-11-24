package com.deepseekharness.app.util;

import static org.junit.Assert.*;
import org.junit.Test;

/** Hermes 运行树与旧的 dsh 树并存：两者都必须是受管路径，且别名只认白名单。 */
public class ManagedRuntimeLayoutHermesTest {
  @Test
  public void hermesTreeIsManaged() {
    assertEquals("opt/hermes", ManagedRuntimeLayout.HERMES);
    assertTrue(ManagedRuntimeLayout.paths().contains(ManagedRuntimeLayout.HERMES));
    assertTrue(ManagedRuntimeLayout.paths().contains(ManagedRuntimeLayout.DSH));
  }

  @Test
  public void hermesEntryAliasIsAllowed() {
    assertTrue(ManagedRuntimeLayout.alias("usr/local/bin/hermes"));
    assertFalse(ManagedRuntimeLayout.alias("usr/local/bin/python3"));
    assertFalse(ManagedRuntimeLayout.alias("usr/local/lib/node_modules/@deepseek-ai/dsh"));
  }

  @Test
  public void hermesPathsStayInsideTheManagedRoot() {
    assertTrue(ManagedRuntimeLayout.allowed("linux/ubuntu/opt/hermes/hermes.sh"));
    assertTrue(ManagedRuntimeLayout.allowed("linux/ubuntu/opt/hermes/python/bin/python3"));
    assertFalse(ManagedRuntimeLayout.allowed("linux/ubuntu/opt/hermes/../../etc/passwd"));
    assertFalse(ManagedRuntimeLayout.allowed("/opt/hermes/hermes.sh"));
  }
}
