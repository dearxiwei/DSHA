package com.deepseekharness.app.util;

import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Hermes 就绪探测。
 *
 * <p>Hermes 绑定 loopback 时没有鉴权门（官方行为：非 loopback 才要求 provider），所以 App 侧不需要
 * 抓启动日志里的鉴权 URL，也不需要换 cookie：只要 {@code GET /api/status} 返回 2xx 即视为就绪。
 *
 * <p>端口由 App 自己选定（{@link WebPortPolicy}）后再传给进程，因此这里不需要「从日志里找端口」。
 */
public final class HermesReadiness {
  private static final String PATH = "/api/status";

  private HermesReadiness() {}

  /** 探测函数：返回 HTTP 状态码，失败返回 -1。放在接口里便于单测替换。 */
  public interface Probe {
    int status(String url);
  }

  public static String url(int port) {
    return "http://127.0.0.1:" + port + PATH;
  }

  public static boolean ready(Probe probe, int port) {
    if (probe == null || port <= 0 || port > 65535) return false;
    int status = probe.status(url(port));
    return status >= 200 && status < 300;
  }

  /** 真机使用的探测实现；超时要短，避免拖住启动线程。 */
  public static Probe httpProbe(int connectMillis, int readMillis) {
    return url -> {
      HttpURLConnection connection = null;
      try {
        connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setConnectTimeout(connectMillis);
        connection.setReadTimeout(readMillis);
        connection.setRequestMethod("GET");
        connection.setUseCaches(false);
        return connection.getResponseCode();
      } catch (IOException | ClassCastException e) {
        return -1;
      } finally {
        if (connection != null) connection.disconnect();
      }
    };
  }
}
