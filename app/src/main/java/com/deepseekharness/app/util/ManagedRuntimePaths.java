package com.deepseekharness.app.util;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.List;

/** One enumeration for installed/staged trees; alias ownership always uses the live root. */
public final class ManagedRuntimePaths {
  private ManagedRuntimePaths() {}

  public enum Marker {
    IDENTITY(".offline-identity"),
    EXTRACTED(".offline-extracted"),
    VERSION(".offline-version"),
    DESCRIPTOR(".runtime-descriptor.json");
    public final String relative;

    Marker(String name) {
      relative = "linux/" + name;
    }
  }

  public interface View {
    File[] list(File directory) throws IOException;

    boolean symbolic(File file) throws IOException;

    boolean exists(File file);

    String canonical(File file) throws IOException;
  }

  public static List<String> enumerate(File listedRoot, File currentRoot, View view)
      throws IOException {
    List<String> paths = new ArrayList<>();
    for (String path : ManagedRuntimeLayout.paths()) paths.add(ManagedRuntimeLayout.ROOT + path);
    File global = new File(listedRoot, "usr/local/lib/node_modules");
    // Hermes 运行树没有 node_modules；缺失时只代表没有包别名，不是环境不可读。
    File[] packages = view.list(global);
    for (File file : packages == null ? new File[0] : packages) {
      if (file.getName().startsWith("@") && !view.symbolic(file)) {
        for (File child : requiredList(view, file))
          addAlias(paths, listedRoot, currentRoot, child, view);
      } else addAlias(paths, listedRoot, currentRoot, file, view);
    }
    for (String name : new String[] {"dsh", "hermes", "tsc", "tsserver"})
      addAlias(paths, listedRoot, currentRoot, new File(listedRoot, "usr/local/bin/" + name), view);
    for (Marker marker : Marker.values()) paths.add(marker.relative);
    return paths;
  }

  private static File[] requiredList(View view, File root) throws IOException {
    File[] list = view.list(root);
    if (list == null) throw new IOException("MANAGED_RUNTIME_UNREADABLE");
    return list;
  }

  private static void addAlias(
      List<String> paths, File listedRoot, File currentRoot, File alias, View view)
      throws IOException {
    if (!view.symbolic(alias)) return;
    String prefix = listedRoot.getAbsolutePath() + File.separator;
    if (!alias.getAbsolutePath().startsWith(prefix)) throw new IOException("MANAGED_ALIAS_PATH");
    String relative =
        alias.getAbsolutePath().substring(prefix.length()).replace(File.separatorChar, '/');
    if (!ManagedRuntimeLayout.alias(relative)) throw new IOException("MANAGED_ALIAS_SCOPE");
    File current = new File(currentRoot, relative);
    boolean owned = false;
    if (view.symbolic(current)) {
      String target = view.canonical(current);
      for (String base :
          new String[] {ManagedRuntimeLayout.DSH, ManagedRuntimeLayout.HERMES}) {
        File owner = new File(currentRoot, base);
        if (!view.exists(owner) && !view.symbolic(owner)) continue;
        String ownerPath = view.canonical(owner);
        if (ownerPath != null && target.startsWith(ownerPath + File.separator)) owned = true;
      }
    }
    if (!view.exists(current) && !view.symbolic(current) || owned)
      paths.add(ManagedRuntimeLayout.ROOT + relative);
  }
}
