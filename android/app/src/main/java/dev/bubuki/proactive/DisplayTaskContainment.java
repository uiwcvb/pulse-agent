package dev.bubuki.proactive;

import android.app.ActivityOptions;
import android.content.ComponentName;
import android.content.res.Resources;
import android.graphics.Rect;
import android.hardware.display.VirtualDisplay;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.Display;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/** Strict task containment & ATM reflection helpers isolating VirtualDisplay tasks from physical Display 0 & phone freeform windows. */
@android.annotation.SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "BlockedPrivateApi"})
final class DisplayTaskContainment {
    volatile int baselineMainTaskId = -1;
    volatile String baselineMainPkg = "";
    volatile long lastVirtualActionUptime = 0L;
    volatile int lastPromotedTaskId = -1;
    volatile String lastPromotedPkg = "";
    volatile boolean containmentSuspended = false;
    final Set<Integer> preActionMainTaskIds = Collections.synchronizedSet(new HashSet<>());

    private static Object getFieldOrMethod(Object obj, String fieldName, String methodName) {
        if (obj == null) return null;
        try {
            Field f = obj.getClass().getField(fieldName);
            Object v = f.get(obj);
            if (v != null) return v;
        } catch (Throwable ignored) {}
        try {
            Method m = obj.getClass().getMethod(methodName);
            Object v = m.invoke(obj);
            if (v != null) return v;
        } catch (Throwable ignored) {}
        return null;
    }

    static int getTaskWindowingMode(Object taskInfo) {
        if (taskInfo == null) return 0;
        try {
            Object cfg = getFieldOrMethod(taskInfo, "configuration", "getConfiguration");
            if (cfg == null) return 0;
            Object winCfg = getFieldOrMethod(cfg, "windowConfiguration", "getWindowConfiguration");
            if (winCfg == null) return 0;
            Object mode = winCfg.getClass().getMethod("getWindowingMode").invoke(winCfg);
            return (mode instanceof Integer) ? (Integer) mode : 0;
        } catch (Throwable ignored) {
            return 0;
        }
    }

    static boolean isFloatingOrMultiWindowTask(Object taskInfo) {
        if (taskInfo == null) return false;
        int mode = getTaskWindowingMode(taskInfo);
        // 1 = WINDOWING_MODE_FULLSCREEN, 0 = UNDEFINED; 2 = PINNED(PiP), 3/4/6 = SPLIT/MULTI, 5 = FREEFORM, >=100 = OEM freeform
        if (mode >= 2) return true;
        if (mode == 0) {
            try {
                Object cfg = getFieldOrMethod(taskInfo, "configuration", "getConfiguration");
                if (cfg != null) {
                    Object winCfg = getFieldOrMethod(cfg, "windowConfiguration", "getWindowConfiguration");
                    if (winCfg != null) {
                        Object b = winCfg.getClass().getMethod("getBounds").invoke(winCfg);
                        if (b instanceof Rect) {
                            Rect r = (Rect) b;
                            DisplayMetrics dm = Resources.getSystem().getDisplayMetrics();
                            if (!r.isEmpty() && dm != null && dm.widthPixels > 0 && dm.heightPixels > 0) {
                                int maxW = Math.max(dm.widthPixels, dm.heightPixels);
                                int minW = Math.min(dm.widthPixels, dm.heightPixels);
                                int bw = Math.min(r.width(), r.height());
                                int bh = Math.max(r.width(), r.height());
                                if (bw < (minW * 88) / 100 && bh < (maxW * 88) / 100) {
                                    return true;
                                }
                            }
                        }
                    }
                }
            } catch (Throwable ignored) {}
        }
        return false;
    }

    static boolean isTaskVisible(Object taskInfo) {
        if (taskInfo == null) return false;
        try {
            Object vis = taskInfo.getClass().getField("isVisible").get(taskInfo);
            if (vis instanceof Boolean) return (Boolean) vis;
        } catch (Throwable ignored) {}
        return false;
    }

    void onBeforeVirtualInteraction(int displayId, Set<String> managedVirtualPackages) {
        if (containmentSuspended || displayId <= 0) return;
        lastVirtualActionUptime = SystemClock.uptimeMillis();
        try {
            String vdTop = DisplayInputHelper.topPackageOnDisplay(displayId);
            if (vdTop != null && !vdTop.isEmpty() && !"dev.bubuki.proactive".equals(vdTop) && !isHomeOrLauncherPackage(vdTop)) {
                managedVirtualPackages.add(vdTop);
            }
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            preActionMainTaskIds.clear();
            baselineMainTaskId = -1;
            baselineMainPkg = "";
            int fallbackTid = -1;
            String fallbackPkg = "";
            int idx = 0;
            for (Object t : queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                int tid = taskIdField.getInt(t);
                if (tid > 0) preActionMainTaskIds.add(tid);

                ComponentName top = (ComponentName) topField.get(t);
                ComponentName base = (ComponentName) baseField.get(t);
                String topPkg = top != null ? top.getPackageName() : "";
                String basePkg = base != null ? base.getPackageName() : "";
                boolean floating = isFloatingOrMultiWindowTask(t);
                boolean visible = isTaskVisible(t);

                if (floating || visible || idx == 0) {
                    if (!topPkg.isEmpty()) managedVirtualPackages.remove(topPkg);
                    if (!basePkg.isEmpty()) managedVirtualPackages.remove(basePkg);
                }

                if (!floating && !managedVirtualPackages.contains(topPkg)) {
                    if (fallbackTid < 0) {
                        fallbackTid = tid;
                        fallbackPkg = topPkg;
                    }
                    if (baselineMainTaskId < 0 && !isHomeOrLauncherPackage(topPkg)) {
                        baselineMainTaskId = tid;
                        baselineMainPkg = topPkg;
                    }
                }
                idx++;
            }
            if (baselineMainTaskId <= 0 && fallbackTid > 0) {
                baselineMainTaskId = fallbackTid;
                baselineMainPkg = fallbackPkg;
            }
        } catch (Throwable ignored) {}
    }

    void scheduleEscapeContainment(
        ScheduledExecutorService timer,
        java.util.function.Supplier<VirtualDisplay> displaySupplier,
        Set<String> managedVirtualPackages,
        int displayId
    ) {
        if (containmentSuspended || displayId <= 0) return;
        final long triggerUptime = SystemClock.uptimeMillis();
        lastVirtualActionUptime = triggerUptime;
        int[] delaysMs = new int[]{70, 200, 450};
        for (int d : delaysMs) {
            timer.schedule(() -> {
                try {
                    containEscapedTasks(displaySupplier.get(), managedVirtualPackages, displayId, triggerUptime);
                } catch (Throwable ignored) {}
            }, d, TimeUnit.MILLISECONDS);
        }
    }

    private void containEscapedTasks(
        VirtualDisplay vd,
        Set<String> managedVirtualPackages,
        int displayId,
        long triggerUptime
    ) {
        if (containmentSuspended) return;
        if (vd == null || vd.getDisplay() == null || vd.getDisplay().getDisplayId() != displayId) return;
        if (SystemClock.uptimeMillis() - triggerUptime > 850L) return;
        try {
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            boolean movedAny = false;
            int inspected = 0;
            for (Object t : queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                int tid = taskIdField.getInt(t);
                int movableTid = extractMovableTaskId(t, taskIdField);
                if (isFloatingOrMultiWindowTask(t)) continue;
                if (tid == baselineMainTaskId || tid == lastPromotedTaskId) break;
                if (preActionMainTaskIds.contains(tid)) {
                    break;
                }
                ComponentName top = (ComponentName) topField.get(t);
                ComponentName base = (ComponentName) baseField.get(t);
                String topPkg = top != null ? top.getPackageName() : "";
                String basePkg = base != null ? base.getPackageName() : "";
                if ("dev.bubuki.proactive".equals(topPkg) || isHomeOrLauncherPackage(topPkg)) break;
                boolean isManagedEscape = !topPkg.isEmpty() && !topPkg.equals(baselineMainPkg)
                    && (managedVirtualPackages.contains(topPkg) || managedVirtualPackages.contains(basePkg));
                if (isManagedEscape) {
                    if (invokeMoveRootTaskToDisplay(atm, movableTid, displayId, true)) {
                        managedVirtualPackages.add(topPkg);
                        movedAny = true;
                    }
                }
                if (++inspected >= 3 || !isManagedEscape) break;
            }
            if (movedAny && baselineMainTaskId > 0) {
                restoreMainDisplayForegroundIfNeeded(atm, baselineMainTaskId, baselineMainPkg, "");
            }
            evictAccidentalFreeformFromVirtualDisplay(displayId, managedVirtualPackages);
        } catch (Throwable ignored) {}
    }

    /** If Android/OEM freeform sidebar accidentally launches a floating mini-window onto VirtualDisplay, return it to Display 0 immediately. */
    static boolean evictAccidentalFreeformFromVirtualDisplay(int displayId, Set<String> managedVirtualPackages) {
        if (displayId <= 0) return false;
        boolean evicted = false;
        try {
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            for (Object t : queryTasksOnDisplay(atm, displayId)) {
                if (t == null || displayField.getInt(t) != displayId) continue;
                if (!isFloatingOrMultiWindowTask(t)) continue;
                int movableTid = extractMovableTaskId(t, taskIdField);
                ComponentName top = (ComponentName) topField.get(t);
                ComponentName base = (ComponentName) baseField.get(t);
                if (managedVirtualPackages != null) {
                    if (top != null) managedVirtualPackages.remove(top.getPackageName());
                    if (base != null) managedVirtualPackages.remove(base.getPackageName());
                }
                if (invokeMoveRootTaskToDisplay(atm, movableTid, Display.DEFAULT_DISPLAY, true)) {
                    evicted = true;
                }
            }
        } catch (Throwable ignored) {}
        return evicted;
    }

    static void releaseTasksAndDisplay(VirtualDisplay vd, Set<String> managedVirtualPackages) {
        if (vd == null) return;
        int dispId = -1;
        try { dispId = vd.getDisplay() != null ? vd.getDisplay().getDisplayId() : -1; } catch (Throwable ignored) {}
        Object atm = null;
        int mainTopTaskId = -1;
        ComponentName mainTopComp = null;
        java.util.Set<String> vdPackages = new java.util.HashSet<>();

        if (dispId > 0) {
            setDisplayRemoveModeDestroy(dispId);
            try {
                atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
                Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
                Field displayField = taskInfoCls.getField("displayId");
                Field taskIdField = taskInfoCls.getField("taskId");
                Field topField = taskInfoCls.getField("topActivity");
                for (Object t : queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                    if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                    mainTopTaskId = taskIdField.getInt(t);
                    mainTopComp = (ComponentName) topField.get(t);
                    break;
                }
                for (Object t : queryTasksOnDisplay(atm, dispId)) {
                    if (t == null || displayField.getInt(t) != dispId) continue;
                    ComponentName top = (ComponentName) topField.get(t);
                    if (top != null && top.getPackageName() != null) vdPackages.add(top.getPackageName());
                }
            } catch (Throwable ignored) {}
        }
        try { vd.release(); } catch (Throwable ignored) {}

        if (dispId > 0 && atm != null && !vdPackages.isEmpty()) {
            int[] checkWaits = new int[]{40, 90, 140};
            String mainTopPkg = mainTopComp != null ? mainTopComp.getPackageName() : "";
            for (int w : checkWaits) {
                try {
                    Thread.sleep(w);
                    String afterTop = DisplayInputHelper.topPackageOnDisplay(Display.DEFAULT_DISPLAY);
                    if (!afterTop.isEmpty() && vdPackages.contains(afterTop) && !afterTop.equals(mainTopPkg)) {
                        if (mainTopTaskId > 0 && !isHomeOrLauncherPackage(mainTopPkg)) {
                            invokeMoveTaskToFront(atm, mainTopTaskId, Display.DEFAULT_DISPLAY);
                        }
                    } else {
                        break;
                    }
                } catch (Throwable ignored) {}
            }
        }
        managedVirtualPackages.clear();
    }

    private static List<?> unwrapTaskList(Object raw) {
        if (raw == null) return java.util.Collections.emptyList();
        if (raw instanceof List<?>) return (List<?>) raw;
        try {
            Object list = raw.getClass().getMethod("getList").invoke(raw);
            if (list instanceof List<?>) return (List<?>) list;
        } catch (Throwable ignored) {}
        return java.util.Collections.emptyList();
    }

    private static List<?> filterTasksByDisplay(List<?> rawList, int targetDisplayId) {
        if (rawList == null || rawList.isEmpty()) return java.util.Collections.emptyList();
        if (targetDisplayId < 0) return rawList;
        List<Object> filtered = new ArrayList<>(rawList.size());
        try {
            Field displayField = Class.forName("android.app.TaskInfo").getField("displayId");
            for (Object t : rawList) {
                if (t != null && displayField.getInt(t) == targetDisplayId) filtered.add(t);
            }
        } catch (Throwable ignored) {
            return rawList;
        }
        return filtered;
    }

    static List<?> queryTasksOnDisplay(Object atm, int targetDisplayId) {
        if (atm == null) return java.util.Collections.emptyList();
        Method getTasks4 = null, getTasks3 = null, getTasks1 = null;
        try {
            for (Method m : atm.getClass().getMethods()) {
                if (m.getName().equals("getTasks")) {
                    int n = m.getParameterTypes().length;
                    if (n == 4) getTasks4 = m;
                    else if (n == 3) getTasks3 = m;
                    else if (n == 1) getTasks1 = m;
                }
            }
        } catch (Throwable ignored) {}
        if (getTasks4 != null) {
            try {
                List<?> res = filterTasksByDisplay(unwrapTaskList(getTasks4.invoke(atm, 50, false, false, targetDisplayId)), targetDisplayId);
                if (!res.isEmpty() || (getTasks3 == null && getTasks1 == null)) return res;
            } catch (Throwable ignored) {}
        }
        if (getTasks3 != null) {
            try {
                List<?> res = filterTasksByDisplay(unwrapTaskList(getTasks3.invoke(atm, 50, false, false)), targetDisplayId);
                if (!res.isEmpty() || getTasks1 == null) return res;
            } catch (Throwable ignored) {}
        }
        if (getTasks1 != null) {
            try {
                return filterTasksByDisplay(unwrapTaskList(getTasks1.invoke(atm, 50)), targetDisplayId);
            } catch (Throwable ignored) {}
        }
        return java.util.Collections.emptyList();
    }

    static boolean isTaskOnDisplay(Object atm, int taskId, int targetDisplayId) {
        if (atm == null || taskId <= 0) return false;
        try {
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            for (Object t : queryTasksOnDisplay(atm, targetDisplayId)) {
                if (t != null && taskIdField.getInt(t) == taskId && displayField.getInt(t) == targetDisplayId) return true;
            }
        } catch (Throwable ignored) {}
        return false;
    }

    private static Bundle buildDisplayActivityOptions(int targetDisplayId) {
        try {
            ActivityOptions opts = ActivityOptions.makeBasic().setLaunchDisplayId(targetDisplayId);
            if (targetDisplayId > 0) {
                try { opts.getClass().getMethod("setLaunchWindowingMode", int.class).invoke(opts, 1); } catch (Throwable ignored) {}
            }
            return opts.toBundle();
        } catch (Throwable ignored) {
            return null;
        }
    }

    static boolean isHomeOrLauncherPackage(String pkg) {
        if (pkg == null) return true;
        String low = pkg.trim().toLowerCase(java.util.Locale.ROOT);
        return low.isEmpty() || low.contains("launcher") || low.contains("home") || "com.android.systemui".equals(low) || "android".equals(low);
    }

    static int extractRootTaskId(Object taskInfo, Field taskIdField) {
        if (taskInfo == null) return -1;
        try {
            int rtid = taskInfo.getClass().getField("rootTaskId").getInt(taskInfo);
            if (rtid > 0) return rtid;
        } catch (Throwable ignored) {}
        try { return taskIdField.getInt(taskInfo); } catch (Throwable ignored) { return -1; }
    }

    /** Return specific leaf taskId so callers never move a shared parent rootTask container across displays. */
    static int extractMovableTaskId(Object taskInfo, Field taskIdField) {
        if (taskInfo == null) return -1;
        try {
            int tid = taskIdField.getInt(taskInfo);
            return tid > 0 ? tid : -1;
        } catch (Throwable ignored) {
            return -1;
        }
    }

    static void setDisplayRemoveModeDestroy(int displayId) {
        if (displayId <= 0) return;
        try {
            Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
            if (wm != null) {
                for (Method m : wm.getClass().getMethods()) {
                    if (m.getName().equals("setRemoveContentMode") && m.getParameterTypes().length == 2) {
                        m.invoke(wm, displayId, 2);
                        break;
                    }
                }
            }
        } catch (Throwable ignored) {}
    }

    private static boolean isProtectedOrSharedContainer(Object atm, int taskId) {
        if (atm == null || taskId <= 0) return true;
        try {
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            List<?> allTasks = queryTasksOnDisplay(atm, -1);
            if (allTasks.isEmpty()) {
                allTasks = queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY);
            }
            int rootMatchCount = 0;
            for (Object t : allTasks) {
                if (t == null) continue;
                int tid = taskIdField.getInt(t);
                int rootTid = extractRootTaskId(t, taskIdField);
                if (rootTid == taskId) {
                    rootMatchCount++;
                }
                if (tid == taskId || rootTid == taskId) {
                    ComponentName top = (ComponentName) topField.get(t);
                    ComponentName base = (ComponentName) baseField.get(t);
                    String topPkg = top != null ? top.getPackageName() : "";
                    String basePkg = base != null ? base.getPackageName() : "";
                    if (isHomeOrLauncherPackage(topPkg) || isHomeOrLauncherPackage(basePkg)
                        || "dev.bubuki.proactive".equals(topPkg) || "dev.bubuki.proactive".equals(basePkg)) {
                        return true;
                    }
                }
            }
            if (rootMatchCount > 1) return true;
        } catch (Throwable ignored) {}
        return false;
    }

    static boolean invokeMoveRootTaskToDisplay(Object atm, int taskId, int targetDisplayId, boolean onTop) {
        if (atm == null || taskId <= 0) return false;
        if (isProtectedOrSharedContainer(atm, taskId)) return false;
        Method moveRoot2 = null, moveRoot3 = null, moveStack2 = null, moveStack3 = null;
        try {
            for (Method m : atm.getClass().getMethods()) {
                String n = m.getName();
                int len = m.getParameterTypes().length;
                if (n.equals("moveRootTaskToDisplay")) {
                    if (len == 2) moveRoot2 = m; else if (len == 3) moveRoot3 = m;
                } else if (n.equals("moveStackToDisplay")) {
                    if (len == 2) moveStack2 = m; else if (len == 3) moveStack3 = m;
                }
            }
        } catch (Throwable ignored) {}
        if (moveRoot2 != null) { try { moveRoot2.invoke(atm, taskId, targetDisplayId); } catch (Throwable ignored) {} if (isTaskOnDisplay(atm, taskId, targetDisplayId)) return true; }
        if (moveRoot3 != null) { try { moveRoot3.invoke(atm, taskId, targetDisplayId, onTop); } catch (Throwable ignored) {} if (isTaskOnDisplay(atm, taskId, targetDisplayId)) return true; }
        if (moveStack2 != null) { try { moveStack2.invoke(atm, taskId, targetDisplayId); } catch (Throwable ignored) {} if (isTaskOnDisplay(atm, taskId, targetDisplayId)) return true; }
        if (moveStack3 != null) { try { moveStack3.invoke(atm, taskId, targetDisplayId, onTop); } catch (Throwable ignored) {} if (isTaskOnDisplay(atm, taskId, targetDisplayId)) return true; }
        return false;
    }

    static boolean invokeMoveTaskToFront(Object atm, int taskId, int targetDisplayId) {
        if (atm == null || taskId <= 0) return false;
        if (targetDisplayId > 0 && !isTaskOnDisplay(atm, taskId, targetDisplayId)) return false;
        if (targetDisplayId == Display.DEFAULT_DISPLAY) {
            try {
                Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
                Field taskIdField = taskInfoCls.getField("taskId");
                Field topField = taskInfoCls.getField("topActivity");
                for (Object t : queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                    if (t != null && taskIdField.getInt(t) == taskId) {
                        if (isFloatingOrMultiWindowTask(t)) return false;
                        ComponentName top = (ComponentName) topField.get(t);
                        if (isHomeOrLauncherPackage(top != null ? top.getPackageName() : "")) return false;
                        break;
                    }
                }
            } catch (Throwable ignored) {}
        }
        Bundle opts = buildDisplayActivityOptions(targetDisplayId);
        if (targetDisplayId == Display.DEFAULT_DISPLAY) {
            try {
                for (Method m : atm.getClass().getMethods()) {
                    if (m.getName().equals("moveTaskToFront") && m.getParameterTypes().length == 5) {
                        m.invoke(atm, null, DisplayInputHelper.SHELL_PACKAGE, taskId, 0, null);
                        return true;
                    }
                }
            } catch (Throwable ignored) {}
            try {
                for (Method m : atm.getClass().getMethods()) {
                    if (m.getName().equals("startActivityFromRecents") && m.getParameterTypes().length == 2) {
                        m.invoke(atm, taskId, opts);
                        return true;
                    }
                }
            } catch (Throwable ignored) {}
            return false;
        }
        try {
            for (Method m : atm.getClass().getMethods()) {
                if (m.getName().equals("startActivityFromRecents") && m.getParameterTypes().length == 2) {
                    m.invoke(atm, taskId, opts);
                    return true;
                }
            }
        } catch (Throwable ignored) {}
        try {
            for (Method m : atm.getClass().getMethods()) {
                if (m.getName().equals("moveTaskToFront") && m.getParameterTypes().length == 5) {
                    m.invoke(atm, null, DisplayInputHelper.SHELL_PACKAGE, taskId, 0, opts);
                    return true;
                }
            }
        } catch (Throwable ignored) {}
        return false;
    }

    static boolean restoreMainDisplayForegroundIfNeeded(
        Object atm,
        int mainBeforeTaskId,
        String mainBeforePkg,
        String movedPkg
    ) {
        if (atm == null || mainBeforeTaskId <= 0 || isHomeOrLauncherPackage(mainBeforePkg)) {
            return false;
        }
        if (movedPkg != null && !movedPkg.isEmpty() && movedPkg.equals(mainBeforePkg)) {
            return false;
        }
        String nowMainTop = DisplayInputHelper.topPackageOnDisplay(Display.DEFAULT_DISPLAY);
        if (isHomeOrLauncherPackage(nowMainTop)) {
            return invokeMoveTaskToFront(atm, mainBeforeTaskId, Display.DEFAULT_DISPLAY);
        }
        return false;
    }

    static void guardAndContainLaunchOnDisplay(
        Object atm,
        int displayId,
        String targetPkg,
        int mainBeforeTaskId,
        String mainBeforePkg,
        Set<Integer> preLaunchMainTaskIds
    ) {
        if (atm == null || displayId <= 0 || "dev.bubuki.proactive".equals(targetPkg)) return;
        try {
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            int[] waitsMs = new int[]{0, 60, 90, 120};
            for (int w : waitsMs) {
                if (w > 0) Thread.sleep(w);
                boolean movedAny = false;
                boolean escapedTopOnMain = false;
                int idx = 0;
                for (Object t : queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                    if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                    int tid = taskIdField.getInt(t);
                    int movableTid = extractMovableTaskId(t, taskIdField);
                    if (isFloatingOrMultiWindowTask(t)) {
                        idx++;
                        continue;
                    }
                    if (preLaunchMainTaskIds != null && preLaunchMainTaskIds.contains(tid)) {
                        idx++;
                        continue;
                    }
                    ComponentName top = (ComponentName) topField.get(t);
                    ComponentName base = (ComponentName) baseField.get(t);
                    String topPkg = top != null ? top.getPackageName() : "";
                    String basePkg = base != null ? base.getPackageName() : "";
                    if ("dev.bubuki.proactive".equals(topPkg) || isHomeOrLauncherPackage(topPkg)) {
                        idx++;
                        continue;
                    }
                    boolean matchesTarget = (targetPkg != null && !targetPkg.isEmpty())
                        ? (targetPkg.equals(topPkg) || targetPkg.equals(basePkg))
                        : (!topPkg.isEmpty() && tid != mainBeforeTaskId && !topPkg.equals(mainBeforePkg));
                    if (matchesTarget && (tid != mainBeforeTaskId || !topPkg.equals(mainBeforePkg))) {
                        if (idx == 0) escapedTopOnMain = true;
                        if (invokeMoveRootTaskToDisplay(atm, movableTid, displayId, true)) {
                            if (targetPkg == null || targetPkg.isEmpty()
                                || !targetPkg.equals(DisplayInputHelper.topPackageOnDisplay(displayId))) {
                                invokeMoveTaskToFront(atm, tid, displayId);
                            }
                            movedAny = true;
                        }
                    }
                    idx++;
                }
                if (movedAny && mainBeforeTaskId > 0) {
                    restoreMainDisplayForegroundIfNeeded(atm, mainBeforeTaskId, mainBeforePkg, targetPkg);
                }
                boolean mainHealthy = mainBeforeTaskId <= 0 || isHomeOrLauncherPackage(mainBeforePkg)
                    || !isHomeOrLauncherPackage(DisplayInputHelper.topPackageOnDisplay(Display.DEFAULT_DISPLAY));
                if (!escapedTopOnMain && mainHealthy
                    && targetPkg != null && !targetPkg.isEmpty()
                    && targetPkg.equals(DisplayInputHelper.topPackageOnDisplay(displayId))) {
                    break;
                }
            }
        } catch (Throwable ignored) {}
    }
}
