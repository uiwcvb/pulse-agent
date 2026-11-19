package dev.bubuki.proactive;

import android.app.ActivityOptions;
import android.app.Application;
import android.app.Instrumentation;
import android.content.*;
import android.content.pm.ApplicationInfo;
import android.graphics.*;
import android.hardware.display.DisplayManager;
import android.os.*;
import android.view.Display;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.KeyEvent;
import android.view.MotionEvent;
import java.lang.reflect.*;
import java.util.List;

/**
 * [Module] DisplayInputHelper
 * [Responsibility] Low-level InputEvent construction & reflection injection, adaptive display
 *                  geometry calculation, AI grid overlay rendering, and ShellContext bootstrap.
 * [Public API]
 *   - displayFlags(sdk, trusted, ownDisplayGroup) / computeAdaptiveDisplaySpec(dm, tierInput)
 *   - setFullscreenWindowingMode(displayId) / setImePolicy(displayId) / ensureAwakeAndUnlocked(displayId)
 *   - injectEvent / injectTap / injectSwipe / injectKey / resolveKeyCode / topPackageOnDisplay
 *   - promoteTopTaskToMainDisplay / pullTaskFromMainToVirtualDisplay / launchOnDisplay / openUriOnDisplay
 *   - systemContext() / drawAiGridOverlay(bmp) / shell(args...)
 */
@android.annotation.SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "BlockedPrivateApi", "SoonBlockedPrivateApi", "WrongConstant"})
final class DisplayInputHelper {
    static final String SHELL_PACKAGE = "com.android.shell";
    static final int SHELL_UID = 2000;

    // DisplayManager.VIRTUAL_DISPLAY_FLAG_* (including hidden constants)
    private static final int FLAG_PUBLIC = 1 << 0, FLAG_SECURE = 1 << 2, FLAG_OWN_CONTENT_ONLY = 1 << 3;
    private static final int FLAG_SUPPORTS_TOUCH = 1 << 6, FLAG_ROTATES_WITH_CONTENT = 1 << 7, FLAG_DESTROY_CONTENT_ON_REMOVAL = 1 << 8;
    private static final int FLAG_TRUSTED = 1 << 10, FLAG_OWN_DISPLAY_GROUP = 1 << 11, FLAG_ALWAYS_UNLOCKED = 1 << 12;
    private static final int FLAG_TOUCH_FEEDBACK_DISABLED = 1 << 13, FLAG_OWN_FOCUS = 1 << 14, FLAG_STEAL_TOP_FOCUS_DISABLED = 1 << 16;
    static final int INJECT_MODE_ASYNC = 0, INJECT_MODE_WAIT_FOR_RESULT = 1, VIRTUAL_KEYBOARD = -1, USER_CURRENT = -2;
    // Cached reflection handles for zero-overhead touch injection & keep-awake
    private static Object sInputManager;
    private static Method sInjectInputEventMethod, sSetDisplayIdMethod;
    static Object sPowerManager;
    static Method sUserActivityMethod;

    static {
        try {
            sSetDisplayIdMethod = InputEvent.class.getMethod("setDisplayId", int.class);
            try {
                sInputManager = Class.forName("android.hardware.input.InputManagerGlobal").getMethod("getInstance").invoke(null);
            } catch (Throwable ignored) {
                sInputManager = Class.forName("android.hardware.input.InputManager").getMethod("getInstance").invoke(null);
            }
            if (sInputManager != null) {
                sInjectInputEventMethod = sInputManager.getClass().getMethod("injectInputEvent", InputEvent.class, int.class);
            }
        } catch (Throwable ignored) {}
        try {
            Object binder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, "power");
            sPowerManager = Class.forName("android.os.IPowerManager$Stub").getMethod("asInterface", IBinder.class).invoke(null, binder);
            if (sPowerManager != null) {
                for (Method m : sPowerManager.getClass().getMethods()) {
                    Class<?>[] p = m.getParameterTypes();
                    if (m.getName().equals("userActivity") && p.length == 4
                        && p[0] == int.class && p[1] == long.class && p[2] == int.class && p[3] == int.class) {
                        sUserActivityMethod = m;
                        break;
                    }
                }
            }
        } catch (Throwable ignored) {}
    }

    private DisplayInputHelper() {}

    static String resolveCallerPackage(int uid) {
        if (uid == 0) {
            return "root";
        }
        if (uid == SHELL_UID) {
            return SHELL_PACKAGE;
        }
        try {
            String pkg = (String) Class.forName("android.app.ActivityThread")
                .getMethod("currentPackageName").invoke(null);
            if (pkg != null && !pkg.isEmpty()) {
                return pkg;
            }
        } catch (Throwable ignored) {}
        try {
            Context sys = systemContext();
            if (sys != null && sys.getPackageManager() != null) {
                String[] pkgs = sys.getPackageManager().getPackagesForUid(uid);
                if (pkgs != null && pkgs.length > 0 && pkgs[0] != null && !pkgs[0].isEmpty()) {
                    return pkgs[0];
                }
            }
        } catch (Throwable ignored) {}
        return "dev.bubuki.proactive";
    }

    static int displayFlags(int sdk, boolean trusted, boolean ownDisplayGroup) {
        return displayFlags(sdk, trusted, ownDisplayGroup, true, true);
    }

    static int displayFlags(int sdk, boolean trusted, boolean ownDisplayGroup, boolean secure) {
        return displayFlags(sdk, trusted, ownDisplayGroup, true, secure);
    }

    static int displayFlags(int sdk, boolean trusted, boolean ownDisplayGroup, boolean alwaysUnlocked, boolean secure) {
        // Strictly omit FLAG_ROTATES_WITH_CONTENT (1 << 7) so DisplayContent never computes
        // a square/landscape mOverrideDisplayInfo (largest app 1552x1552) that causes first-frame oversized fonts.
        // Include FLAG_SECURE (1 << 2) by default so Alipay/WeChat payment/verification windows can render on VirtualDisplay.
        int flags = FLAG_PUBLIC | FLAG_OWN_CONTENT_ONLY | FLAG_SUPPORTS_TOUCH
            | FLAG_DESTROY_CONTENT_ON_REMOVAL;
        if (secure) {
            flags |= FLAG_SECURE;
        }
        if (sdk >= Build.VERSION_CODES.TIRAMISU) {
            flags |= FLAG_TOUCH_FEEDBACK_DISABLED;
            if (trusted) {
                flags |= FLAG_TRUSTED;
                if (alwaysUnlocked) {
                    flags |= FLAG_ALWAYS_UNLOCKED;
                }
                if (ownDisplayGroup) {
                    flags |= FLAG_OWN_DISPLAY_GROUP;
                }
            }
        }
        if (sdk >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE && trusted) {
            flags |= FLAG_OWN_FOCUS | FLAG_STEAL_TOP_FOCUS_DISABLED;
        }
        return flags;
    }

    static int roundTo16(float value, int minVal, int maxVal) {
        int clamped = Math.max(minVal, Math.min(maxVal, Math.round(value)));
        return ((clamped + 8) / 16) * 16;
    }

    static int[] computeAdaptiveDisplaySpec(DisplayManager dm, String tierInput) {
        String tier = (tierInput == null || tierInput.isEmpty()) ? "compact" : tierInput.trim().toLowerCase();
        int rw = 1080, rh = 2400, realDpi = 480;
        try {
            Display mainDisp = dm != null ? dm.getDisplay(Display.DEFAULT_DISPLAY) : null;
            if (mainDisp != null) {
                android.util.DisplayMetrics realMetrics = new android.util.DisplayMetrics();
                mainDisp.getRealMetrics(realMetrics);
                if (realMetrics.widthPixels >= 320 && realMetrics.heightPixels >= 320) {
                    rw = realMetrics.widthPixels;
                    rh = realMetrics.heightPixels;
                }
                if (realMetrics.densityDpi >= 120 && realMetrics.densityDpi <= 800) {
                    realDpi = realMetrics.densityDpi;
                }
                try {
                    Display.Mode mode = mainDisp.getMode();
                    if (mode != null && mode.getPhysicalWidth() >= 720 && mode.getPhysicalHeight() >= 720) {
                        rw = mode.getPhysicalWidth();
                        rh = mode.getPhysicalHeight();
                    }
                } catch (Throwable ignored) {}
            }
        } catch (Throwable ignored) {}
        try {
            Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
            if (wm != null) {
                Point initialSize = new Point();
                wm.getClass().getMethod("getInitialDisplaySize", int.class, Point.class).invoke(wm, Display.DEFAULT_DISPLAY, initialSize);
                if (initialSize.x >= 720 && initialSize.y >= 720) {
                    rw = initialSize.x;
                    rh = initialSize.y;
                }
                int baseDpi = (Integer) wm.getClass().getMethod("getInitialDisplayDensity", int.class).invoke(wm, Display.DEFAULT_DISPLAY);
                if (baseDpi >= 120 && baseDpi <= 800) {
                    realDpi = baseDpi;
                }
            }
        } catch (Throwable ignored) {}

        int shortSide = Math.min(rw, rh);
        int longSide = Math.max(rw, rh);

        // Lock VirtualDisplay geometry & DPI 1:1 with Display 0's physical portrait dimensions and densityDpi!
        // In Android AOSP (ActivityRecord.ensureActivityConfiguration), moving a Task between Display 0 and
        // VirtualDisplay via moveRootTaskToDisplay triggers a full Activity Relaunch (restarting Alipay/WeChat)
        // if densityDpi, screenWidthDp, or smallestScreenWidthDp differs. Matching Display 0 1:1 guarantees
        // 100% lossless zero-relaunch task migration (promote / pull_back) AND native font scaling.
        int targetW = shortSide;
        int targetH = longSide;
        int targetDpi = realDpi;

        int targetPreviewShort;
        if ("compact".equals(tier) || "small".equals(tier) || "mini".equals(tier)) {
            targetPreviewShort = 460;
        } else if ("large".equals(tier) || "hd".equals(tier) || "full".equals(tier)) {
            targetPreviewShort = 720;
        } else {
            targetPreviewShort = 560;
        }
        return new int[]{targetW, targetH, targetDpi, targetPreviewShort};
    }

    static void setFullscreenWindowingMode(int displayId) {
        setFullscreenWindowingMode(displayId, 0, 0, 0);
    }

    static void setFullscreenWindowingMode(int displayId, int width, int height, int densityDpi) {
        try {
            Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
            try {
                wm.getClass().getMethod("setWindowingMode", int.class, int.class).invoke(wm, displayId, 1);
            } catch (Throwable ignored) {}
            try {
                wm.getClass().getMethod("fixToUserRotation", int.class, int.class).invoke(wm, displayId, 2);
            } catch (Throwable ignored) {}
            try {
                wm.getClass().getMethod("freezeDisplayRotation", int.class, int.class).invoke(wm, displayId, 0);
            } catch (Throwable ignored) {}
        } catch (Throwable t) {
            try {
                shell("wm", "set-display-windowing-mode", "-d", String.valueOf(displayId), "1");
            } catch (Throwable ignored) {}
        }
    }

    static void setImePolicy(int displayId) {
        if (!setDisplayImePolicy(displayId, 0)) {
            setDisplayImePolicy(displayId, 1);
        }
    }

    static boolean setDisplayImePolicy(int displayId, int policy) {
        try {
            Object wm = Class.forName("android.view.WindowManagerGlobal").getMethod("getWindowManagerService").invoke(null);
            wm.getClass().getMethod("setDisplayImePolicy", int.class, int.class).invoke(wm, displayId, policy);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    static void pokeDisplayAwake(int displayId) {
        // Intentionally no-op: FLAG_ALWAYS_UNLOCKED on VirtualDisplay keeps rendering active without waking physical backlight.
    }

    static void ensureAwakeAndUnlocked(int displayId) {
        pokeDisplayAwake(displayId);
        // Never inject KEYCODE_WAKEUP onto Display 0 or dismiss Display 0's keyguard;
        // VirtualDisplay uses FLAG_ALWAYS_UNLOCKED so waking only targets displayId (> 0).
        if (displayId <= 0) return;
        boolean interactive = true;
        try {
            if (sPowerManager != null) {
                interactive = (Boolean) sPowerManager.getClass().getMethod("isInteractive").invoke(sPowerManager);
            }
        } catch (Throwable ignored) {}
        if (!interactive) {
            try {
                injectKey(displayId, KeyEvent.KEYCODE_WAKEUP);
            } catch (Throwable ignored) {}
        }
    }

    static boolean injectEvent(InputEvent event, int displayId, int mode) {
        try {
            if (sSetDisplayIdMethod != null) {
                sSetDisplayIdMethod.invoke(event, displayId);
            } else {
                event.getClass().getMethod("setDisplayId", int.class).invoke(event, displayId);
            }
            if (sInputManager != null && sInjectInputEventMethod != null) {
                return (Boolean) sInjectInputEventMethod.invoke(sInputManager, event, mode);
            }
            Object im = Class.forName("android.hardware.input.InputManagerGlobal").getMethod("getInstance").invoke(null);
            return (Boolean) im.getClass().getMethod("injectInputEvent", InputEvent.class, int.class).invoke(im, event, mode);
        } catch (Throwable t) {
            return false;
        }
    }

    static boolean injectTap(Handler inputHandler, Runnable onFrameSchedule, int displayId, int x, int y) {
        inputHandler.removeCallbacksAndMessages(null);
        long now = SystemClock.uptimeMillis();
        MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, x, y, 0);
        down.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        boolean ok;
        try {
            ok = injectEvent(down, displayId, INJECT_MODE_ASYNC);
        } finally {
            down.recycle();
        }
        if (!ok) return false;
        inputHandler.postDelayed(() -> {
            long upTime = SystemClock.uptimeMillis();
            MotionEvent up = MotionEvent.obtain(now, upTime, MotionEvent.ACTION_UP, x, y, 0);
            up.setSource(InputDevice.SOURCE_TOUCHSCREEN);
            try {
                if (!injectEvent(up, displayId, INJECT_MODE_ASYNC)) {
                    injectEvent(up, displayId, INJECT_MODE_ASYNC);
                }
            } finally {
                up.recycle();
            }
            if (onFrameSchedule != null) onFrameSchedule.run();
        }, 32L);
        return true;
    }

    static boolean injectSwipe(Handler inputHandler, Runnable onFrameSchedule, int displayId, int x1, int y1, int x2, int y2, int durationMs) {
        inputHandler.removeCallbacksAndMessages(null);
        final int steps = Math.max(6, Math.min(60, durationMs / 14));
        final long downTime = SystemClock.uptimeMillis();
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[]{new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[]{new MotionEvent.PointerCoords()};
        coords[0].pressure = 1f;
        coords[0].size = 1f;
        coords[0].x = x1;
        coords[0].y = y1;

        MotionEvent down = MotionEvent.obtain(downTime, downTime, MotionEvent.ACTION_DOWN, 1, props, coords, 0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        boolean ok;
        try {
            ok = injectEvent(down, displayId, INJECT_MODE_ASYNC);
        } finally {
            down.recycle();
        }
        if (!ok) return false;

        for (int i = 1; i <= steps; i++) {
            final int stepIdx = i;
            final long delayMs = Math.round(((float) i / steps) * durationMs);
            inputHandler.postDelayed(() -> {
                MotionEvent.PointerProperties[] p = new MotionEvent.PointerProperties[]{new MotionEvent.PointerProperties()};
                p[0].id = 0;
                p[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
                MotionEvent.PointerCoords[] c = new MotionEvent.PointerCoords[]{new MotionEvent.PointerCoords()};
                c[0].pressure = 1f;
                c[0].size = 1f;
                long eventTime = SystemClock.uptimeMillis();
                if (stepIdx < steps) {
                    float t = (float) stepIdx / steps;
                    float smooth = t * t * (3f - 2f * t);
                    c[0].x = x1 + (x2 - x1) * smooth;
                    c[0].y = y1 + (y2 - y1) * smooth;
                    MotionEvent move = MotionEvent.obtain(downTime, eventTime, MotionEvent.ACTION_MOVE, 1, p, c, 0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
                    try {
                        injectEvent(move, displayId, INJECT_MODE_ASYNC);
                    } finally {
                        move.recycle();
                    }
                    if (stepIdx % 2 == 0 && onFrameSchedule != null) onFrameSchedule.run();
                } else {
                    c[0].x = x2;
                    c[0].y = y2;
                    MotionEvent up = MotionEvent.obtain(downTime, eventTime, MotionEvent.ACTION_UP, 1, p, c, 0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
                    try {
                        if (!injectEvent(up, displayId, INJECT_MODE_ASYNC)) {
                            injectEvent(up, displayId, INJECT_MODE_ASYNC);
                        }
                    } finally {
                        up.recycle();
                    }
                    if (onFrameSchedule != null) onFrameSchedule.run();
                }
            }, delayMs);
        }
        return true;
    }

    static boolean injectKey(int displayId, int keyCode) {
        long now = SystemClock.uptimeMillis();
        KeyEvent down = new KeyEvent(now, now, KeyEvent.ACTION_DOWN, keyCode, 0, 0, VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD);
        KeyEvent up = KeyEvent.changeAction(down, KeyEvent.ACTION_UP);
        if (!injectEvent(down, displayId, INJECT_MODE_WAIT_FOR_RESULT)) return false;
        return injectEvent(up, displayId, INJECT_MODE_WAIT_FOR_RESULT) || injectEvent(up, displayId, INJECT_MODE_WAIT_FOR_RESULT);
    }

    static void injectClearField(int displayId) {
        long now = SystemClock.uptimeMillis();
        int meta = KeyEvent.META_CTRL_ON | KeyEvent.META_CTRL_LEFT_ON;
        KeyEvent selDown = new KeyEvent(now, now, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_A, 0, meta, VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD);
        KeyEvent selUp = KeyEvent.changeAction(selDown, KeyEvent.ACTION_UP);
        injectEvent(selDown, displayId, INJECT_MODE_WAIT_FOR_RESULT);
        injectEvent(selUp, displayId, INJECT_MODE_WAIT_FOR_RESULT);
        try { Thread.sleep(60L); } catch (InterruptedException ignored) {}
        injectKey(displayId, KeyEvent.KEYCODE_DEL);
        try { Thread.sleep(60L); } catch (InterruptedException ignored) {}
    }

    static int resolveKeyCode(String key, int explicitCode) throws Exception {
        int keyCode;
        if (explicitCode > 0 && key.isEmpty()) {
            keyCode = explicitCode;
        } else {
            switch (key) {
                case "home": keyCode = KeyEvent.KEYCODE_BACK; break;
                case "back": keyCode = KeyEvent.KEYCODE_BACK; break;
                case "enter":
                case "66": keyCode = KeyEvent.KEYCODE_ENTER; break;
                case "search":
                case "84": keyCode = KeyEvent.KEYCODE_SEARCH; break;
                case "tab": keyCode = KeyEvent.KEYCODE_TAB; break;
                case "delete":
                case "backspace": keyCode = KeyEvent.KEYCODE_DEL; break;
                case "up": keyCode = KeyEvent.KEYCODE_DPAD_UP; break;
                case "down": keyCode = KeyEvent.KEYCODE_DPAD_DOWN; break;
                case "left": keyCode = KeyEvent.KEYCODE_DPAD_LEFT; break;
                case "right": keyCode = KeyEvent.KEYCODE_DPAD_RIGHT; break;
                default:
                    if (explicitCode > 0) {
                        keyCode = explicitCode;
                    } else {
                        throw new Exception("不支持此工作区按键: " + key);
                    }
            }
        }
        if (keyCode == KeyEvent.KEYCODE_HOME || keyCode == KeyEvent.KEYCODE_APP_SWITCH) {
            keyCode = KeyEvent.KEYCODE_BACK;
        }
        return keyCode;
    }

    static String topPackageOnDisplay(int displayId) {
        try {
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field topField = taskInfoCls.getField("topActivity");
            for (Object t : DisplayTaskContainment.queryTasksOnDisplay(atm, displayId)) {
                if (t == null) continue;
                if (displayField.getInt(t) == displayId) {
                    ComponentName top = (ComponentName) topField.get(t);
                    if (top != null) return top.getPackageName();
                }
            }
        } catch (Throwable ignored) {}
        return "";
    }

    static int[] promoteTopTaskToMainDisplay(int displayId) {
        if (displayId <= 0) return new int[]{-1, 0};
        try {
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            for (Object t : DisplayTaskContainment.queryTasksOnDisplay(atm, displayId)) {
                if (t == null || displayField.getInt(t) != displayId) continue;
                int tid = taskIdField.getInt(t);
                ComponentName top = (ComponentName) topField.get(t);
                String topPkg = top != null ? top.getPackageName() : "";
                if ("dev.bubuki.proactive".equals(topPkg)) continue;
                boolean moved = DisplayTaskContainment.invokeMoveRootTaskToDisplay(atm, tid, Display.DEFAULT_DISPLAY, true);
                if (moved) {
                    DisplayTaskContainment.invokeMoveTaskToFront(atm, tid, Display.DEFAULT_DISPLAY);
                    return new int[]{tid, 1};
                }
                return new int[]{tid, 0};
            }
        } catch (Throwable ignored) {}
        return new int[]{-1, 0};
    }

    static int pullTaskFromMainToVirtualDisplay(int displayId, int preferredTaskId, String preferredPkg) {
        if (displayId <= 0) return -1;
        boolean hasExplicitFilter = preferredTaskId > 0 || (preferredPkg != null && !preferredPkg.trim().isEmpty());
        if (!hasExplicitFilter) return -1;
        String wantPkg = preferredPkg != null ? preferredPkg.trim() : "";
        try {
            Object atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");
            for (Object t : DisplayTaskContainment.queryTasksOnDisplay(atm, Display.DEFAULT_DISPLAY)) {
                if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                int tid = taskIdField.getInt(t);
                ComponentName top = (ComponentName) topField.get(t);
                ComponentName base = (ComponentName) baseField.get(t);
                String topPkg = top != null ? top.getPackageName() : "";
                String basePkg = base != null ? base.getPackageName() : "";
                if ("dev.bubuki.proactive".equals(topPkg) || DisplayTaskContainment.isHomeOrLauncherPackage(topPkg)) continue;
                if (preferredTaskId > 0 && tid == preferredTaskId) {
                    int movableTid = DisplayTaskContainment.extractMovableTaskId(t, taskIdField);
                    if (DisplayTaskContainment.invokeMoveRootTaskToDisplay(atm, movableTid, displayId, true)) {
                        DisplayTaskContainment.invokeMoveTaskToFront(atm, tid, displayId);
                        return tid;
                    }
                    return -1;
                }
                if (preferredTaskId <= 0 && !wantPkg.isEmpty() && (wantPkg.equals(topPkg) || wantPkg.equals(basePkg))) {
                    if (DisplayTaskContainment.isFloatingOrMultiWindowTask(t)) continue;
                    int movableTid = DisplayTaskContainment.extractMovableTaskId(t, taskIdField);
                    if (DisplayTaskContainment.invokeMoveRootTaskToDisplay(atm, movableTid, displayId, true)) {
                        DisplayTaskContainment.invokeMoveTaskToFront(atm, tid, displayId);
                        return tid;
                    }
                    return -1;
                }
            }
        } catch (Throwable ignored) {}
        return -1;
    }

    private static String resolveEnabledLauncherComponent(String pkg, int userId) {
        try {
            String resolved = shell(
                "cmd", "package", "resolve-activity", "--brief",
                "--user", String.valueOf(userId),
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                pkg
            );
            if (resolved != null) {
                for (String line : resolved.split("\\r?\\n")) {
                    String s = line.trim();
                    if (s.contains("/") && !s.startsWith("priority=") && !s.contains(" ")) return s;
                }
            }
        } catch (Throwable ignored) {}
        return "";
    }

    private static final class Display0Snapshot {
        Object atm;
        int mainBeforeTaskId = -1;
        String mainBeforePkg = "";
        int existingMainTaskId = -1;
        boolean existingMainIsFloatingOrVisible = false;
        final java.util.Set<Integer> preLaunchMainTaskIds = new java.util.HashSet<>();
    }

    private static Display0Snapshot snapshotDisplay0BeforeLaunch(String pkg) {
        Display0Snapshot snap = new Display0Snapshot();
        try {
            snap.atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
            Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
            Field displayField = taskInfoCls.getField("displayId");
            Field taskIdField = taskInfoCls.getField("taskId");
            Field topField = taskInfoCls.getField("topActivity");
            Field baseField = taskInfoCls.getField("baseActivity");

            for (Object t : DisplayTaskContainment.queryTasksOnDisplay(snap.atm, Display.DEFAULT_DISPLAY)) {
                if (t == null || displayField.getInt(t) != Display.DEFAULT_DISPLAY) continue;
                int tid = taskIdField.getInt(t);
                int movableTid = DisplayTaskContainment.extractMovableTaskId(t, taskIdField);
                if (tid > 0) snap.preLaunchMainTaskIds.add(tid);

                ComponentName top = (ComponentName) topField.get(t);
                ComponentName base = (ComponentName) baseField.get(t);
                String topPkg = top != null ? top.getPackageName() : "";
                String basePkg = base != null ? base.getPackageName() : "";
                boolean floating = DisplayTaskContainment.isFloatingOrMultiWindowTask(t);
                boolean visible = DisplayTaskContainment.isTaskVisible(t);
                if (snap.mainBeforeTaskId < 0 && !floating) {
                    snap.mainBeforeTaskId = tid;
                    snap.mainBeforePkg = topPkg;
                }
                if (pkg != null && !pkg.isEmpty() && !"dev.bubuki.proactive".equals(pkg)
                    && (pkg.equals(topPkg) || pkg.equals(basePkg))) {
                    if (snap.existingMainTaskId < 0 && movableTid > 0) snap.existingMainTaskId = movableTid;
                    if (floating || visible || tid == snap.mainBeforeTaskId) {
                        snap.existingMainIsFloatingOrVisible = true;
                    }
                }
            }
        } catch (Throwable ignored) {}
        return snap;
    }

    static String launchOnDisplay(int displayId, String component, int userId) throws Exception {
        if (displayId <= 0) {
            throw new Exception("拒绝在主屏 (displayId=" + displayId + ") 启动虚拟屏应用");
        }
        String[] parts = component.split("/");
        String pkg = parts[0];
        Display0Snapshot snap = snapshotDisplay0BeforeLaunch(pkg);
        int existingVirtualTaskId = -1;
        boolean useMultipleTaskFlags = false;

        try {
            if (snap.atm != null && !"dev.bubuki.proactive".equals(pkg)) {
                Class<?> taskInfoCls = Class.forName("android.app.TaskInfo");
                Field displayField = taskInfoCls.getField("displayId");
                Field taskIdField = taskInfoCls.getField("taskId");
                Field topField = taskInfoCls.getField("topActivity");
                Field baseField = taskInfoCls.getField("baseActivity");

                for (Object vt : DisplayTaskContainment.queryTasksOnDisplay(snap.atm, displayId)) {
                    if (vt == null || displayField.getInt(vt) != displayId) continue;
                    int vtid = taskIdField.getInt(vt);
                    ComponentName vtop = (ComponentName) topField.get(vt);
                    ComponentName vbase = (ComponentName) baseField.get(vt);
                    String vtopPkg = vtop != null ? vtop.getPackageName() : "";
                    String vbasePkg = vbase != null ? vbase.getPackageName() : "";
                    if (pkg.equals(vtopPkg) || pkg.equals(vbasePkg)) {
                        existingVirtualTaskId = vtid;
                        break;
                    }
                }
                if (existingVirtualTaskId > 0) {
                    if (!pkg.equals(topPackageOnDisplay(displayId))) {
                        DisplayTaskContainment.invokeMoveTaskToFront(snap.atm, existingVirtualTaskId, displayId);
                        for (int i = 0; i < 4 && !pkg.equals(topPackageOnDisplay(displayId)); i++) Thread.sleep(45L);
                    }
                    if (pkg.equals(topPackageOnDisplay(displayId))) {
                        return "already_on_virtual_display_brought_to_front (" + existingVirtualTaskId + ")";
                    }
                }
            }
        } catch (Throwable ignored) {}

        if (snap.existingMainIsFloatingOrVisible && !"dev.bubuki.proactive".equals(pkg)) {
            throw new Exception("该应用 (" + pkg + ") 当前正在手机主屏或小窗运行，为保护主屏小窗不掉入虚拟屏、不变全屏，已跳过抢占");
        }

        boolean omitWindowingMode = snap.existingMainTaskId > 0;
        if (snap.existingMainTaskId > 0 && !"dev.bubuki.proactive".equals(pkg)) {
            try {
                if (DisplayTaskContainment.invokeMoveRootTaskToDisplay(snap.atm, snap.existingMainTaskId, displayId, true)) {
                    DisplayTaskContainment.invokeMoveTaskToFront(snap.atm, snap.existingMainTaskId, displayId);
                    for (int i = 0; i < 4 && !pkg.equals(topPackageOnDisplay(displayId)); i++) Thread.sleep(45L);
                    DisplayTaskContainment.restoreMainDisplayForegroundIfNeeded(snap.atm, snap.mainBeforeTaskId, snap.mainBeforePkg, pkg);
                    if (pkg.equals(topPackageOnDisplay(displayId))) {
                        return "pre_migrated_dormant_task_to_virtual_display (" + snap.existingMainTaskId + ")";
                    }
                }
            } catch (Throwable ignored) {}
            // Pre-migration did not complete cleanly; fall back to MULTIPLE_TASK without forcing Display 0 fullscreen.
            useMultipleTaskFlags = true;
        }

        String flagsHex = "dev.bubuki.proactive".equals(pkg)
            ? "0x14000000"
            : (useMultipleTaskFlags ? "0x18000000" : "0x10200000");
        // Omit --windowingMode 1 when an existing Display 0 task exists so singleTask routing never forces it fullscreen.
        String output = omitWindowingMode
            ? shell(
                "am", "start", "--display", String.valueOf(displayId),
                "--user", String.valueOf(userId), "-f", flagsHex, "-n", component
            )
            : shell(
                "am", "start", "--display", String.valueOf(displayId), "--windowingMode", "1",
                "--user", String.valueOf(userId), "-f", flagsHex, "-n", component
            );
        if (output.contains("Error") || output.contains("Exception") || output.contains("does not exist") || output.contains("Permission Denial") || output.contains("Unknown option")) {
            if (output.contains("Unknown option")) {
                output = shell(
                    "am", "start", "--display", String.valueOf(displayId),
                    "--user", String.valueOf(userId), "-f", flagsHex, "-n", component
                );
            }
            if (output.contains("Error") || output.contains("Exception") || output.contains("does not exist") || output.contains("Permission Denial")) {
                String altComp = resolveEnabledLauncherComponent(pkg, userId);
                if (!altComp.isEmpty() && !altComp.equals(component)) {
                    output = shell(
                        "am", "start", "--display", String.valueOf(displayId),
                        "--user", String.valueOf(userId), "-f", flagsHex, "-n", altComp
                    );
                }
            }
        }
        DisplayTaskContainment.guardAndContainLaunchOnDisplay(snap.atm, displayId, pkg, snap.mainBeforeTaskId, snap.mainBeforePkg, snap.preLaunchMainTaskIds);
        if ((output.contains("Error") || output.contains("Exception")) && !pkg.equals(topPackageOnDisplay(displayId))) {
            throw new Exception(output.substring(0, Math.min(300, output.length())));
        }
        return output;
    }

    static String openUriOnDisplay(int displayId, String uri, int userId) throws Exception {
        if (displayId <= 0) {
            throw new Exception("拒绝在主屏 (displayId=" + displayId + ") 打开链接");
        }
        String topPkg = topPackageOnDisplay(displayId);
        String lower = uri != null ? uri.toLowerCase(java.util.Locale.ROOT) : "";
        boolean isWeixinWeb = lower.startsWith("http://") || lower.startsWith("https://");
        if (isWeixinWeb) {
            try {
                String host = android.net.Uri.parse(uri).getHost();
                isWeixinWeb = host != null && (host.equalsIgnoreCase("weixin.qq.com") || host.toLowerCase(java.util.Locale.ROOT).endsWith(".weixin.qq.com"));
            } catch (Throwable ignored) {
                isWeixinWeb = false;
            }
        }
        boolean preferWeChat = "com.tencent.mm".equals(topPkg) || isWeixinWeb;
        Display0Snapshot snap = snapshotDisplay0BeforeLaunch(preferWeChat ? "com.tencent.mm" : "");
        if (preferWeChat && !snap.existingMainIsFloatingOrVisible) {
            try {
                String wxOut = shell(
                    "am", "start", "--display", String.valueOf(displayId), "--user", String.valueOf(userId),
                    "-f", "0x18000000",
                    "-n", "com.tencent.mm/.plugin.webview.ui.tools.WebViewUI",
                    "-e", "rawUrl", uri
                );
                if (!wxOut.contains("Error") && !wxOut.contains("Exception")) {
                    DisplayTaskContainment.guardAndContainLaunchOnDisplay(
                        snap.atm, displayId, "com.tencent.mm",
                        snap.mainBeforeTaskId, snap.mainBeforePkg, snap.preLaunchMainTaskIds
                    );
                    return wxOut;
                }
            } catch (Throwable ignored) {}
        }
        String output = shell("am", "start", "--display", String.valueOf(displayId), "--user", String.valueOf(userId), "-f", "0x18000000", "-a", "android.intent.action.VIEW", "-d", uri);
        DisplayTaskContainment.guardAndContainLaunchOnDisplay(
            snap.atm, displayId, "",
            snap.mainBeforeTaskId, snap.mainBeforePkg, snap.preLaunchMainTaskIds
        );
        if (output.contains("Error") || output.contains("Exception")) {
            throw new Exception(output.substring(0, Math.min(300, output.length())));
        }
        return output;
    }

    static String shell(String... args) throws Exception {
        return RootCommands.run(args);
    }

    static String checkedInputShell(String... args) throws Exception {
        String out = RootCommands.run(args);
        if (out != null && (out.contains("Error") || out.contains("Exception") || out.contains("Invalid")
                || out.contains("Killed") || out.contains("Permission Denial"))) {
            throw new Exception(out.substring(0, Math.min(250, out.length())));
        }
        return out;
    }

    static Context systemContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Object thread = null;
        try {
            thread = at.getMethod("currentActivityThread").invoke(null);
        } catch (Throwable ignored) {}
        if (thread == null) {
            try {
                Constructor<?> ctor = at.getDeclaredConstructor(); ctor.setAccessible(true);
                thread = ctor.newInstance();
                Field sCur = at.getDeclaredField("sCurrentActivityThread"); sCur.setAccessible(true); sCur.set(null, thread);
                Field mSys = at.getDeclaredField("mSystemThread"); mSys.setAccessible(true); mSys.setBoolean(thread, true);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    try {
                        Class<?> cc = Class.forName("android.app.ConfigurationController"), internal = Class.forName("android.app.ActivityThreadInternal");
                        Constructor<?> ccCtor = cc.getDeclaredConstructor(internal); ccCtor.setAccessible(true);
                        Field mCc = at.getDeclaredField("mConfigurationController"); mCc.setAccessible(true); mCc.set(thread, ccCtor.newInstance(thread));
                    } catch (Throwable ignored) {}
                }
                try {
                    Class<?> abd = Class.forName("android.app.ActivityThread$AppBindData");
                    Constructor<?> abdCtor = abd.getDeclaredConstructor(); abdCtor.setAccessible(true);
                    Object data = abdCtor.newInstance();
                    ApplicationInfo ai = new ApplicationInfo(); ai.packageName = SHELL_PACKAGE;
                    Field appInfoField = abd.getDeclaredField("appInfo"); appInfoField.setAccessible(true); appInfoField.set(data, ai);
                    Field boundField = at.getDeclaredField("mBoundApplication"); boundField.setAccessible(true); boundField.set(thread, data);
                } catch (Throwable ignored) {}
            } catch (Throwable fallback) { thread = at.getMethod("systemMain").invoke(null); }
        }
        Context sys = (Context) at.getDeclaredMethod("getSystemContext").invoke(thread);
        try {
            Field initApp = at.getDeclaredField("mInitialApplication");
            initApp.setAccessible(true);
            if (initApp.get(thread) == null) {
                Application app = Instrumentation.newApplication(Application.class, sys);
                initApp.set(thread, app);
            }
        } catch (Throwable ignored) {}
        return sys;
    }

    static void drawAiGridOverlay(Bitmap bmp) {
        if (bmp == null) return;
        int w = bmp.getWidth(), h = bmp.getHeight(), cols = 6, rows = 10;
        Canvas c = new Canvas(bmp);
        Paint linePaint = new Paint(Paint.ANTI_ALIAS_FLAG); linePaint.setColor(0x5500E5FF); linePaint.setStrokeWidth(1f);
        Paint bgPaint = new Paint(Paint.ANTI_ALIAS_FLAG); bgPaint.setColor(0x77000000);
        Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG); textPaint.setColor(0xDDFFFFFF); textPaint.setTextSize(Math.max(10f, w / 42f));
        for (int col = 1; col < cols; col++) { float x = w * col / (float) cols; c.drawLine(x, 0, x, h, linePaint); }
        for (int row = 1; row < rows; row++) { float y = h * row / (float) rows; c.drawLine(0, y, w, y, linePaint); }
        String[] letters = {"A", "B", "C", "D", "E", "F"};
        for (int r = 0; r < rows; r++) {
            for (int col = 0; col < cols; col++) {
                String label = letters[col] + (r + 1);
                float cx = w * (col + 0.08f) / cols, cy = h * (r + 0.22f) / rows;
                c.drawRect(cx - 2f, cy - textPaint.getTextSize(), cx + textPaint.measureText(label) + 2f, cy + 3f, bgPaint);
                c.drawText(label, cx, cy, textPaint);
            }
        }
    }
}
