package com.deepseekharness.app.util;

import static org.junit.Assert.*;
import org.junit.Test;

public class HermesReadinessTest {
  @Test
  public void acceptsOnlyTwoHundredFamilies() {
    assertTrue(HermesReadiness.ready(url -> 200, 9119));
    assertTrue(HermesReadiness.ready(url -> 204, 9119));
    assertFalse(HermesReadiness.ready(url -> 301, 9119));
    assertFalse(HermesReadiness.ready(url -> 401, 9119));
    assertFalse(HermesReadiness.ready(url -> 500, 9119));
    assertFalse(HermesReadiness.ready(url -> -1, 9119));
  }

  @Test
  public void probesLoopbackStatusPath() {
    String[] seen = new String[1];
    HermesReadiness.ready(
        url -> {
          seen[0] = url;
          return 200;
        },
        9119);
    assertEquals("http://127.0.0.1:9119/api/status", seen[0]);
  }

  @Test
  public void rejectsInvalidPortsWithoutProbing() {
    HermesReadiness.Probe never = url -> {
      throw new AssertionError("不应探测非法端口");
    };
    assertFalse(HermesReadiness.ready(never, 0));
    assertFalse(HermesReadiness.ready(never, -1));
    assertFalse(HermesReadiness.ready(never, 65536));
    assertFalse(HermesReadiness.ready(null, 9119));
  }
}
