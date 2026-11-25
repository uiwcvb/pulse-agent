package dev.bubuki.proactive;

import android.content.*;
import android.content.res.Resources;
import android.hardware.display.*;
import android.os.*;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.Surface;
import org.json.*;
import java.lang.reflect.*;
import java.util.concurrent.*;

/** VirtualDisplay lifecycle, DisplayFramePipeline integration, JSON RPC dispatcher & containment. */
@android.annotation.SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "BlockedPrivateApi", "SoonBlockedPrivateApi", "WrongConstant"})
final class DisplayRuntime {
    private static final String SHELL_PACKAGE = DisplayInputHelper.SHELL_PACKAGE;
    private static final int SHELL_UID = DisplayInputHelper.SHELL_UID;
    private static final long KEEP_AWAKE_WINDOW_MS = 45_000L;

    private final HandlerThread thread = new HandlerThread("pulse-independent-display");
    private final Handler handler;
    private final HandlerThread inputThread = new HandlerThread("pulse-virtual-input");
    private final Handler inputHandler;
    private final Context context;
    private volatile VirtualDisplay display;
    private final DisplayFramePipeline framePipeline = new DisplayFramePipeline();
    private final ScheduledExecutorService awakeTimer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "pulse-display-awake");
        t.setDaemon(true);
        return t;
    });
    private volatile ScheduledFuture<?> awakeTask;
    private final Object displayOpLock = new Object();
    private volatile long displayCreateEpoch = 0L;
    private volatile int previewShortSide = 460;
    private volatile long liveTouchDownTime = 0L;

    private volatile boolean trustedDisplay = true;
    private volatile boolean releasedToBackground = false;
    private RootClipboard clipboard;
    private volatile int width = 1472, height = 3232, densityDpi = 480;
    private volatile String activeTier = "compact";

    // Sandbox task containment & main-display anti-jump state
    private final java.util.Set<String> managedVirtualPackages = java.util.Collections.synchronizedSet(new java.util.HashSet<>());
    private final DisplayTaskContainment containment = new DisplayTaskContainment();

    DisplayRuntime(Context base) {
        final int myUid = android.os.Process.myUid();
        final String callerPackage = DisplayInputHelper.resolveCallerPackage(myUid);
        context = new ContextWrapper(base) {
            @Override public String getPackageName() { return callerPackage; }
            @Override public String getOpPackageName() { return callerPackage; }
            @Override public Context getApplicationContext() { return this; }
            @Override public Resources getResources() {
                Resources sys = Resources.getSystem();
                try {
                    android.content.res.Configuration cfg = new android.content.res.Configuration(sys.getConfiguration());
                    cfg.densityDpi = densityDpi;
                    cfg.fontScale = 1.0f;
                    cfg.screenWidthDp = Math.round((Math.min(width, height) * 160f) / Math.max(120, densityDpi));
                    cfg.smallestScreenWidthDp = cfg.screenWidthDp;
                    android.util.DisplayMetrics dm = new android.util.DisplayMetrics();
                    dm.setTo(sys.getDisplayMetrics());
                    dm.densityDpi = densityDpi;
                    dm.density = densityDpi / 160f;
                    dm.scaledDensity = dm.density;
                    dm.widthPixels = width;
                    dm.heightPixels = height;
                    return new Resources(sys.getAssets(), dm, cfg);
                } catch (Throwable ignored) {
                    return sys;
                }
            }
            @Override public AttributionSource getAttributionSource() {
                return new AttributionSource.Builder(myUid).setPackageName(callerPackage).build();
            }
        };
        thread.start();
        handler = new Handler(thread.getLooper());
        inputThread.start();
        inputHandler = new Handler(inputThread.getLooper());
        clipboard = new RootClipboard(context);
    }

    String execute(String command) {
        // Fast lock-free path for preview, status, and live touch stream so frame polling & dragging NEVER queue behind RPCs
        try {
            JSONObject fastCmd = new JSONObject(command);
            String fastOp = fastCmd.optString("op", "");
            if ("preview".equals(fastOp)) {
                return framePipeline.fastPreview(fastCmd, this::state).toString();
            }
            if ("status".equals(fastOp)) {
                return state().toString();
            }
            if ("touch".equals(fastOp)) {
                return fastTouch(fastCmd).toString();
            }
            if ("release".equals(fastOp)) {
                final boolean destroyReq = !fastCmd.optBoolean("keep_alive", false)
                    || fastCmd.optBoolean("destroy", false)
                    || fastCmd.optBoolean("user_close", false);
                if (destroyReq) {
                    releasedToBackground = true;
                    framePipeline.setReleasedToBackground(true);
                    stopKeepAwake();
                    FutureTask<String> relTask = new FutureTask<>(() -> run(fastCmd).toString());
                    handler.post(relTask);
                    try {
                        return relTask.get(1500, TimeUnit.MILLISECONDS);
                    } catch (Throwable timeoutErr) {
                        synchronized (displayOpLock) {
                            displayCreateEpoch++;
                            release(true);
                        }
                        return state().put("ok", true).put("released", true).put("destroyed", true).put("forced", true).toString();
                    }
                }
            }
        } catch (Throwable ignored) {}

        FutureTask<String> task = new FutureTask<>(() -> {
            try {
                return run(new JSONObject(command)).toString();
            } catch (Throwable e) {
                Throwable cause = e instanceof InvocationTargetException ? ((InvocationTargetException) e).getTargetException() : e;
                return new JSONObject().put("error", cause.getClass().getSimpleName() + ": " + cause.getMessage()).toString();
            }
        });
        handler.post(task);
        try {
            long waitSec = 18;
            try {
                JSONObject c = new JSONObject(command);
                if ("shell".equals(c.optString("op"))) {
                    waitSec = Math.max(18, Math.min(90, c.optInt("timeout_sec", 18) + 4));
                }
            } catch (Throwable ignored) {}
            return task.get(waitSec, TimeUnit.SECONDS);
        } catch (Exception e) {
            return "{\"error\":\"虚拟屏操作超时\"}";
        }
    }

    private void scheduleFrameProcess() {
        framePipeline.scheduleFrameProcess();
    }

    private JSONObject fastTouch(JSONObject cmd) throws Exception {
        VirtualDisplay vd = this.display;
        if (vd == null || vd.getDisplay() == null || vd.getDisplay().getDisplayId() <= 0 || inputHandler == null) {
            return state().put("ok", false);
        }
        releasedToBackground = false;
        framePipeline.setReleasedToBackground(false);
        final int displayId = vd.getDisplay().getDisplayId();
        final int rot = vd.getDisplay().getRotation();
        final int lw = (rot % 2 == 0) ? width : height;
        final int lh = (rot % 2 == 0) ? height : width;
        final String actionStr = cmd.optString("action", "move").trim().toLowerCase(java.util.Locale.ROOT);
        final float x = (float) Math.max(0, Math.min(Math.max(1, lw) - 1, cmd.optInt("x", 0)));
        final float y = (float) Math.max(0, Math.min(Math.max(1, lh) - 1, cmd.optInt("y", 0)));
        boolean posted = inputHandler.post(() -> {
            try {
                long now = SystemClock.uptimeMillis();
                framePipeline.markLiveTouch();
                int action;
                if ("down".equals(actionStr)) {
                    liveTouchDownTime = now;
                    action = MotionEvent.ACTION_DOWN;
                    onBeforeVirtualInteraction(displayId);
                } else if ("up".equals(actionStr)) {
                    action = MotionEvent.ACTION_UP;
                } else if ("cancel".equals(actionStr)) {
                    action = MotionEvent.ACTION_CANCEL;
                } else {
                    action = MotionEvent.ACTION_MOVE;
                }
                long downT = liveTouchDownTime > 0L ? liveTouchDownTime : now;
                MotionEvent.PointerProperties[] p = new MotionEvent.PointerProperties[]{new MotionEvent.PointerProperties()};
                p[0].id = 0;
                p[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
                MotionEvent.PointerCoords[] c = new MotionEvent.PointerCoords[]{new MotionEvent.PointerCoords()};
                c[0].pressure = (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) ? 0f : 1f;
                c[0].size = 1f;
                c[0].x = x;
                c[0].y = y;
                MotionEvent ev = MotionEvent.obtain(downT, now, action, 1, p, c, 0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
                try {
                    DisplayInputHelper.injectEvent(ev, displayId, DisplayInputHelper.INJECT_MODE_ASYNC);
                } finally {
                    ev.recycle();
                }
                if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
                    liveTouchDownTime = 0L;
                }
                scheduleFrameProcess();
            } catch (Throwable ignored) {}
        });
        return new JSONObject().put("uid", android.os.Process.myUid()).put("ok", posted).put("displayId", displayId);
    }

    @android.annotation.SuppressLint("WrongConstant")
    private JSONObject run(JSONObject cmd) throws Exception {
        String op = cmd.getString("op");
        if (op.equals("create")) {
            int myUid = android.os.Process.myUid();
            if (myUid != 0 && myUid != 2000) throw new Exception("虚拟屏需要 Root (uid 0) 或 Shell 权限");
            String requestedTier = cmd.optString("tier", activeTier).trim().toLowerCase(java.util.Locale.ROOT);
            if (requestedTier.isEmpty()) requestedTier = "compact";
            synchronized (displayOpLock) {
                long myEpoch = displayCreateEpoch;
                Constructor<DisplayManager> ctor = DisplayManager.class.getDeclaredConstructor(Context.class);
                ctor.setAccessible(true);
                DisplayManager dm = ctor.newInstance(context);
                int[] spec = DisplayInputHelper.computeAdaptiveDisplaySpec(dm, requestedTier);
                int wantW = cmd.optInt("width", spec[0]);
                int wantH = cmd.optInt("height", spec[1]);
                int wantDpi = cmd.optInt("dpi", spec[2]);
                if (wantDpi < 120 || wantDpi > 800) wantDpi = spec[2];

                boolean explicitGeom = cmd.has("width") || cmd.has("height") || cmd.has("dpi");
                if (display != null && display.getDisplay() != null && display.getDisplay().getDisplayId() > 0 && !cmd.optBoolean("recreate", false)) {
                    releasedToBackground = false;
                    activeTier = requestedTier;
                    previewShortSide = cmd.optInt("preview_short_side", spec[3]);
                    int existingId = display.getDisplay().getDisplayId();
                    if (explicitGeom && (wantW != width || wantH != height || wantDpi != densityDpi)) {
                        // Only tear down & recreate when caller explicitly requested different geometry
                        release(true);
                    } else {
                        DisplayInputHelper.ensureAwakeAndUnlocked(existingId);
                        startKeepAwake(existingId);
                        String curTop = DisplayInputHelper.topPackageOnDisplay(existingId);
                        if (!cmd.optBoolean("skip_home", false)
                            && (curTop == null || curTop.isEmpty() || "com.android.launcher".equals(curTop))) {
                            try {
                                DisplayInputHelper.launchOnDisplay(existingId, "dev.bubuki.proactive/.DisplayTestActivity", 0);
                            } catch (Throwable ignored) {}
                        }
                        framePipeline.reattachIfPresent(display, previewShortSide);
                        return state().put("newly_created", false).put("reused_display", true);
                    }
                }
                if (myEpoch != displayCreateEpoch) {
                    return state().put("newly_created", false).put("aborted_by_release", true);
                }
                release(true);
                releasedToBackground = false;

                activeTier = requestedTier;
                width = wantW;
                height = wantH;
                densityDpi = wantDpi;
                previewShortSide = cmd.optInt("preview_short_side", spec[3]);
                if (width < 128 || width > 4096 || height < 128 || height > 4096) throw new Exception("虚拟屏尺寸超出范围");

                Surface surface = framePipeline.prepareSurface(width, height, previewShortSide);
                // 5-tier cascade: keep FLAG_TRUSTED (+ OWN_FOCUS / STEAL_TOP_FOCUS_DISABLED)
                // even when OEMs reject OWN_DISPLAY_GROUP or ALWAYS_UNLOCKED.
                final boolean[][] tiers = {
                    {true,  true,  true,  true},  // Tier 1: trusted + ownGroup + alwaysUnlocked + secure
                    {true,  false, true,  true},  // Tier 2: trusted + alwaysUnlocked + secure
                    {true,  false, false, true},  // Tier 3: trusted + secure
                    {false, false, false, true},  // Tier 4: untrusted + secure
                    {false, false, false, false}, // Tier 5: untrusted + non-secure
                };
                Throwable lastCreateErr = null;
                display = null;
                for (boolean[] tCfg : tiers) {
                    VirtualDisplay candidate = null;
                    try {
                        trustedDisplay = tCfg[0];
                        int flags = DisplayInputHelper.displayFlags(
                            Build.VERSION.SDK_INT, tCfg[0], tCfg[1], tCfg[2], tCfg[3]
                        );
                        candidate = dm.createVirtualDisplay("Pulse-Agent", width, height, densityDpi, surface, flags);
                        if (candidate != null && candidate.getDisplay() != null && candidate.getDisplay().getDisplayId() > 0) {
                            display = candidate;
                            break;
                        }
                        if (candidate != null) {
                            try { candidate.release(); } catch (Throwable ignored) {}
                        }
                    } catch (Throwable err) {
                        if (candidate != null) {
                            try { candidate.release(); } catch (Throwable ignored) {}
                        }
                        lastCreateErr = err;
                        display = null;
                    }
                }
                if (display == null) {
                    release(true);
                    if (lastCreateErr instanceof Exception) throw (Exception) lastCreateErr;
                    throw new Exception(
                        lastCreateErr != null && lastCreateErr.getMessage() != null
                            ? lastCreateErr.getMessage()
                            : "createVirtualDisplay failed",
                        lastCreateErr
                    );
                }
            if (display == null || display.getDisplay().getDisplayId() <= 0) {
                release(true);
                throw new Exception("系统没有返回独立虚拟屏");
            }
            int displayId = display.getDisplay().getDisplayId();
            containment.containmentSuspended = false;
            DisplayTaskContainment.setDisplayRemoveModeDestroy(displayId);
            DisplayInputHelper.setFullscreenWindowingMode(displayId, width, height, densityDpi);
            DisplayInputHelper.setImePolicy(displayId);
            DisplayInputHelper.ensureAwakeAndUnlocked(displayId);
            startKeepAwake(displayId);
            if (!cmd.optBoolean("skip_home", false)) {
                try {
                    DisplayInputHelper.launchOnDisplay(displayId, "dev.bubuki.proactive/.DisplayTestActivity", 0);
                } catch (Throwable ignored) {}
            }

            framePipeline.bindDisplay(display);
            return state().put("newly_created", true).put("reused_display", false);
            }
        } else if (op.equals("resize")) {
            synchronized (displayOpLock) {
                requireDisplay();
                Constructor<DisplayManager> ctor = DisplayManager.class.getDeclaredConstructor(Context.class);
                ctor.setAccessible(true);
                DisplayManager dm = ctor.newInstance(context);
                String requestedTier = cmd.optString("tier", activeTier).trim().toLowerCase(java.util.Locale.ROOT);
                if (requestedTier.isEmpty()) requestedTier = "compact";
                int[] spec = DisplayInputHelper.computeAdaptiveDisplaySpec(dm, requestedTier);
                activeTier = requestedTier;
                previewShortSide = cmd.optInt("preview_short_side", spec[3]);
                framePipeline.setPreviewShortSide(previewShortSide);
                if (cmd.optBoolean("force_display_resize", false)) {
                    int newW = cmd.optInt("width", spec[0]);
                    int newH = cmd.optInt("height", spec[1]);
                    int newDpi = cmd.optInt("dpi", spec[2]);
                    VirtualDisplay vd = this.display;
                    if (vd != null) {
                        if (newW != width || newH != height) {
                            width = newW;
                            height = newH;
                            framePipeline.replaceSurfaceForResize(vd, newW, newH);
                        }
                        densityDpi = newDpi;
                        vd.resize(width, height, densityDpi);
                        if (vd.getDisplay() != null) {
                            DisplayInputHelper.setFullscreenWindowingMode(vd.getDisplay().getDisplayId(), width, height, densityDpi);
                        }
                    }
                }
                scheduleFrameProcess();
                return state().put("resized", true).put("tier", activeTier);
            }
        } else if (op.equals("promote") || op.equals("expand_to_foreground") || op.equals("to_foreground") || op.equals("fullscreen")) {
            if (display == null || display.getDisplay() == null || display.getDisplay().getDisplayId() <= 0) {
                return state().put("ok", false).put("error", "虚拟屏未启动");
            }
            int id = display.getDisplay().getDisplayId();
            String topPkg = DisplayInputHelper.topPackageOnDisplay(id);
            if (topPkg != null && !topPkg.isEmpty()) {
                managedVirtualPackages.remove(topPkg);
            }
            containment.lastVirtualActionUptime = 0L;
            int[] res = DisplayInputHelper.promoteTopTaskToMainDisplay(id);
            if (res[1] == 1) {
                containment.containmentSuspended = true;
                containment.lastPromotedTaskId = res[0];
                containment.lastPromotedPkg = topPkg != null ? topPkg : "";
            }
            releasedToBackground = false;
            return state()
                .put("ok", res[1] == 1)
                .put("promoted", res[1] == 1)
                .put("promoted_task_id", res[0])
                .put("promoted_package", containment.lastPromotedPkg)
                .put("kept_alive", true);
        } else if (op.equals("pull_back") || op.equals("to_virtual")) {
            if (display == null || display.getDisplay() == null || display.getDisplay().getDisplayId() <= 0) {
                return state().put("ok", false).put("error", "虚拟屏未启动");
            }
            int id = display.getDisplay().getDisplayId();
            containment.containmentSuspended = false;
            String wantPkg = cmd.optString("package", cmd.optString("pkg", containment.lastPromotedPkg)).trim();
            int wantTid = cmd.optInt("task_id", containment.lastPromotedTaskId);
            if (wantTid <= 0 && wantPkg.isEmpty()) {
                return state().put("ok", false).put("pulled_back", false).put("noop", true);
            }
            int pulledTid = DisplayInputHelper.pullTaskFromMainToVirtualDisplay(id, wantTid, wantPkg);
            if (pulledTid > 0) {
                String nowTop = DisplayInputHelper.topPackageOnDisplay(id);
                if (nowTop != null && !nowTop.isEmpty() && !"dev.bubuki.proactive".equals(nowTop)) {
                    managedVirtualPackages.add(nowTop);
                }
                containment.lastPromotedTaskId = -1;
                containment.lastPromotedPkg = "";
            }
            scheduleFrameProcess();
            return state()
                .put("ok", pulledTid > 0)
                .put("pulled_back", pulledTid > 0)
                .put("pulled_task_id", pulledTid);
        } else if (op.equals("release")) {
            if (display == null) {
                containment.containmentSuspended = false;
                return state().put("ok", true).put("noop", true).put("released", true).put("destroyed", true).put("released_to_background", true);
            }
            boolean keepAlive = cmd.optBoolean("keep_alive", false);
            boolean destroy = !keepAlive || cmd.optBoolean("destroy", false) || cmd.optBoolean("user_close", false);
            if (!destroy) {
                releasedToBackground = false;
                return state().put("ok", true).put("released_to_background", false).put("kept_alive", true);
            }
            containment.containmentSuspended = false;
            release(true);
            return state().put("ok", true).put("released", true).put("released_to_background", true).put("destroyed", true);
        } else if (op.equals("preview")) {
            return framePipeline.fastPreview(cmd, this::state);
        } else if (op.equals("shell")) {
            String rawCmd = cmd.optString("command", cmd.optString("cmd", "")).trim();
            if (rawCmd.isEmpty()) throw new Exception("shell 操作需要提供 command");
            int activeVdId = (display != null && display.getDisplay() != null) ? display.getDisplay().getDisplayId() : -1;
            boolean touchesUi = activeVdId > 0 && (rawCmd.contains("am ") || rawCmd.contains("input") || rawCmd.contains("cmd activity") || rawCmd.contains("monkey"));
            if (touchesUi) onBeforeVirtualInteraction(activeVdId);
            JSONObject shRes = RootCommands.runShell(rawCmd, cmd.optInt("timeout_sec", 18));
            if (touchesUi) {
                scheduleEscapeContainment(activeVdId);
                scheduleFrameProcess();
            }
            return shRes;
        } else if (op.equals("clipboard") || op.equals("clipboard_get") || op.equals("clipboard_set")) {
            boolean isSet = op.equals("clipboard_set")
                || "set".equalsIgnoreCase(cmd.optString("action", ""))
                || cmd.has("text") || cmd.has("value");
            if (isSet) {
                String text = cmd.optString("text", cmd.optString("value", ""));
                clipboard.setText(text);
                return new JSONObject()
                    .put("uid", android.os.Process.myUid())
                    .put("ok", true)
                    .put("op", "clipboard_set")
                    .put("length", text.length());
            } else {
                String text = clipboard.getText();
                return new JSONObject()
                    .put("uid", android.os.Process.myUid())
                    .put("ok", true)
                    .put("op", "clipboard_get")
                    .put("text", text);
            }
        } else if (op.equals("capture")) {
            int capId = requireDisplay();
            if (awakeTask == null) startKeepAwake(capId);
            return framePipeline.capture(cmd, this::state);
        } else if (op.equals("tap")) {
            int id = requireDisplay(), x = cmd.getInt("x"), y = cmd.getInt("y");
            checkTarget(cmd, id);
            checkPoint(x, y);
            onBeforeVirtualInteraction(id);
            if (!DisplayInputHelper.injectTap(inputHandler, this::scheduleFrameProcess, id, x, y)) {
                DisplayInputHelper.checkedInputShell("input", "-d", String.valueOf(id), "tap", String.valueOf(x), String.valueOf(y));
            }
            scheduleEscapeContainment(id);
            return state().put("clicked", true).put("x", x).put("y", y);
        } else if (op.equals("swipe")) {
            int id = requireDisplay();
            checkTarget(cmd, id);
            int x1 = cmd.has("x1") ? cmd.getInt("x1") : cmd.getInt("x");
            int y1 = cmd.has("y1") ? cmd.getInt("y1") : cmd.getInt("y");
            int x2 = cmd.getInt("x2"), y2 = cmd.getInt("y2");
            checkPoint(x1, y1);
            checkPoint(x2, y2);
            int duration = cmd.optInt("duration_ms", 220);
            if (duration < 40 || duration > 2500) throw new Exception("滑动时长须为 40–2500 ms");
            onBeforeVirtualInteraction(id);
            if (!DisplayInputHelper.injectSwipe(inputHandler, this::scheduleFrameProcess, id, x1, y1, x2, y2, duration)) {
                DisplayInputHelper.checkedInputShell("input", "-d", String.valueOf(id), "swipe", String.valueOf(x1), String.valueOf(y1), String.valueOf(x2), String.valueOf(y2), String.valueOf(duration));
            }
            scheduleEscapeContainment(id);
            return state().put("swiped", true).put("x1", x1).put("y1", y1).put("x2", x2).put("y2", y2);
        } else if (op.equals("text")) {
            int id = requireDisplay();
            checkTarget(cmd, id);
            String text = cmd.optString("text", "");
            boolean clear = cmd.optBoolean("clear", false);
            if ((!clear && text.isEmpty()) || text.length() > 4000) throw new Exception("一次输入 1–4000 字（或设置 clear=true 清空）");
            onBeforeVirtualInteraction(id);
            if (clipboard == null) clipboard = new RootClipboard(context);
            clipboard.paste(id, text, clear);
            scheduleEscapeContainment(id);
            return state().put("typed", true).put("cleared", clear).put("characters", text.length());
        } else if (op.equals("key")) {
            int id = requireDisplay();
            checkTarget(cmd, id);
            String key = cmd.optString("key", "").trim().toLowerCase(java.util.Locale.ROOT);
            int explicitCode = cmd.optInt("keycode", cmd.optInt("key_code", -1));
            if ("home".equals(key) || explicitCode == android.view.KeyEvent.KEYCODE_HOME) {
                onBeforeVirtualInteraction(id);
                DisplayInputHelper.launchOnDisplay(id, "dev.bubuki.proactive/.DisplayTestActivity", 0);
                scheduleFrameProcess();
                return state().put("key", "home").put("keycode", android.view.KeyEvent.KEYCODE_HOME).put("home_launched", true);
            }
            int keyCode = DisplayInputHelper.resolveKeyCode(key, explicitCode);
            if (key.isEmpty() && explicitCode > 0) key = String.valueOf(explicitCode);
            if (keyCode == android.view.KeyEvent.KEYCODE_BACK) {
                String currentTop = DisplayInputHelper.topPackageOnDisplay(id);
                if (currentTop == null || currentTop.isEmpty() || "dev.bubuki.proactive".equals(currentTop)) {
                    return state()
                        .put("key", key.isEmpty() ? "back" : key)
                        .put("keycode", keyCode)
                        .put("at_virtual_root", true);
                }
            }
            onBeforeVirtualInteraction(id);
            if (!DisplayInputHelper.injectKey(id, keyCode)) {
                DisplayInputHelper.checkedInputShell("input", "-d", String.valueOf(id), "keyevent", String.valueOf(keyCode));
            }
            scheduleEscapeContainment(id);
            return state().put("key", key).put("keycode", keyCode);
        } else if (op.equals("open_uri") || op.equals("open_url")) {
            int id = requireDisplay();
            String uri = cmd.optString("uri", cmd.optString("url", cmd.optString("text", ""))).trim();
            if (uri.isEmpty()) throw new Exception("请提供要打开的 URI/URL");
            onBeforeVirtualInteraction(id);
            String out = DisplayInputHelper.openUriOnDisplay(id, uri, cmd.optInt("user_id", 0));
            scheduleEscapeContainment(id);
            String topPkg = DisplayInputHelper.topPackageOnDisplay(id);
            return state().put("opened_uri", uri).put("output", out).put("package", topPkg != null ? topPkg : "");
        } else if (op.equals("long_press")) {
            int id = requireDisplay(), x = cmd.getInt("x"), y = cmd.getInt("y");
            int duration = Math.max(450, Math.min(2000, cmd.optInt("duration_ms", 650)));
            checkTarget(cmd, id);
            checkPoint(x, y);
            onBeforeVirtualInteraction(id);
            if (!DisplayInputHelper.injectSwipe(inputHandler, this::scheduleFrameProcess, id, x, y, x, y, duration)) {
                DisplayInputHelper.checkedInputShell("input", "-d", String.valueOf(id), "swipe", String.valueOf(x), String.valueOf(y), String.valueOf(x), String.valueOf(y), String.valueOf(duration));
            }
            scheduleEscapeContainment(id);
            return state().put("long_pressed", true).put("x", x).put("y", y);
        } else if (op.equals("ime_policy")) {
            int id = requireDisplay();
            Object rawPolicy = cmd.opt("policy");
            int policyVal = 0;
            if (rawPolicy instanceof Number) {
                policyVal = ((Number) rawPolicy).intValue();
            } else {
                String p = cmd.optString("policy", "local").trim().toLowerCase(java.util.Locale.ROOT);
                if ("fallback".equals(p) || "1".equals(p)) policyVal = 1;
                else if ("hide".equals(p) || "2".equals(p)) policyVal = 2;
                else if ("local".equals(p) || "0".equals(p) || p.isEmpty()) policyVal = 0;
                else throw new Exception("未知 IME 策略: " + p + " (支持 local/fallback/hide)");
            }
            if (policyVal < 0 || policyVal > 2) throw new Exception("IME 策略范围须为 0..2");
            boolean ok = DisplayInputHelper.setDisplayImePolicy(id, policyVal);
            if (!ok) throw new Exception("设置虚拟屏 IME 策略失败: policy=" + policyVal);
            return state().put("ime_policy", policyVal);
        } else if (op.equals("launch")) {
            int id = requireDisplay();
            checkTarget(cmd, id);
            String component = cmd.getString("component");
            if (!component.matches("[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+")) throw new Exception("填写 package/.Activity 形式的组件名");
            if (component.startsWith("dev.bubuki.proactive/") && !component.endsWith(".DisplayTestActivity")) {
                throw new Exception("禁止将本客户端主界面移入虚拟屏");
            }
            String pkg = component.split("/")[0];
            onBeforeVirtualInteraction(id);
            if (!"dev.bubuki.proactive".equals(pkg)) {
                managedVirtualPackages.add(pkg);
            }
            int userId = cmd.optInt("user_id", 0);
            String output = DisplayInputHelper.launchOnDisplay(id, component, userId);
            scheduleEscapeContainment(id);
            String topPkg = DisplayInputHelper.topPackageOnDisplay(id);
            return state().put("output", output).put("package", topPkg != null && !topPkg.isEmpty() ? topPkg : pkg);
        } else if (op.equals("ui_tree")) {
            int id = requireDisplay();
            framePipeline.markInteraction();
            if (awakeTask == null) startKeepAwake(id);
            int rot = display.getDisplay().getRotation();
            int lw = rot % 2 == 0 ? width : height;
            int lh = rot % 2 == 0 ? height : width;
            return VirtualDisplayUiTreeHelper.dumpDisplay(id, lw, lh);
        } else if (op.equals("keep_awake")) {
            if (display != null && display.getDisplay() != null && display.getDisplay().getDisplayId() > 0) {
                int id = display.getDisplay().getDisplayId();
                framePipeline.markInteraction();
                if (awakeTask == null) startKeepAwake(id);
                DisplayInputHelper.pokeDisplayAwake(id, true);
                return state().put("ok", true).put("awake_poked", true);
            }
            return state().put("ok", true).put("awake_poked", false);
        } else if (!op.equals("status")) {
            throw new Exception("未知虚拟屏操作: " + op + "（支持 create/release/status/capture/ui_tree/tap/swipe/long_press/text/key/launch/open_uri/ime_policy/shell/clipboard/promote/pull_back/keep_awake）");
        }
        return state();
    }

    private void onBeforeVirtualInteraction(int displayId) {
        framePipeline.markInteraction();
        if (awakeTask == null && display != null) {
            startKeepAwake(displayId);
        }
        containment.onBeforeVirtualInteraction(displayId, managedVirtualPackages);
    }

    private void scheduleEscapeContainment(int displayId) {
        containment.scheduleEscapeContainment(awakeTimer, () -> this.display, managedVirtualPackages, displayId);
    }

    private void startKeepAwake(int displayId) {
        stopKeepAwake();
        framePipeline.markInteraction();
        awakeTask = awakeTimer.scheduleWithFixedDelay(() -> {
            try {
                if (SystemClock.uptimeMillis() - framePipeline.lastInteractionAtMs() > KEEP_AWAKE_WINDOW_MS) {
                    stopKeepAwake();
                    return;
                }
                if (!releasedToBackground && !containment.containmentSuspended && display != null) {
                    DisplayTaskContainment.evictAccidentalFreeformFromVirtualDisplay(displayId, managedVirtualPackages);
                }
                DisplayInputHelper.pokeDisplayAwake(displayId);
            } catch (Throwable ignored) {}
        }, 0, 2500, TimeUnit.MILLISECONDS);
    }

    private synchronized void stopKeepAwake() {
        ScheduledFuture<?> task = awakeTask;
        awakeTask = null;
        if (task != null) {
            task.cancel(false);
        }
    }

    private int requireDisplay() throws Exception {
        if (display == null || display.getDisplay().getDisplayId() <= 0) {
            throw new Exception("先创建独立虚拟屏；禁止注入 display 0");
        }
        return display.getDisplay().getDisplayId();
    }

    private void checkTarget(JSONObject command, int id) throws Exception {
        if (command.optInt("display_id", id) != id) throw new Exception("displayId 不属于当前独立工作区");
    }

    private void checkPoint(int x, int y) throws Exception {
        int rotation = display.getDisplay().getRotation();
        int w = rotation % 2 == 0 ? width : height, h = rotation % 2 == 0 ? height : width;
        if (x < 0 || y < 0 || x >= w || y >= h) throw new Exception("坐标超出独立虚拟屏");
    }

    private JSONObject state() throws Exception {
        return DisplayFrameCodec.buildStateJson(
            this.display,
            width,
            height,
            densityDpi,
            releasedToBackground,
            activeTier,
            containment.lastPromotedTaskId,
            containment.lastPromotedPkg,
            trustedDisplay
        );
    }

    private void release(boolean destroy) {
        synchronized (displayOpLock) {
            if (destroy) {
                displayCreateEpoch++;
            }
            stopKeepAwake();
            inputHandler.removeCallbacksAndMessages(null);
            if (!destroy && display != null) {
                releasedToBackground = true;
                framePipeline.setReleasedToBackground(true);
                return;
            }
            releasedToBackground = false;
            containment.lastPromotedTaskId = -1;
            containment.lastPromotedPkg = "";
            containment.preActionMainTaskIds.clear();
            if (display != null) {
                VirtualDisplay vd = display;
                display = null;
                DisplayTaskContainment.releaseTasksAndDisplay(vd, managedVirtualPackages);
            }
            framePipeline.releaseBuffers();
        }
    }

    void close() {
        if (display != null) {
            execute("{\"op\":\"release\",\"destroy\":true}");
        }
        awakeTimer.shutdownNow();
        inputThread.quitSafely();
        thread.quitSafely();
        framePipeline.quit();
    }

    static Context systemContext() throws Exception {
        return DisplayInputHelper.systemContext();
    }
}
