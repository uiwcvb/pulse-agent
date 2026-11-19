package dev.bubuki.proactive;

import android.content.*;
import android.content.res.Resources;
import android.graphics.*;
import android.hardware.display.*;
import android.media.Image;
import android.media.ImageReader;
import android.os.*;
import android.util.Base64;
import android.view.Display;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.Surface;
import org.json.*;
import java.io.*;
import java.lang.reflect.*;
import java.util.concurrent.*;

/** VirtualDisplay lifecycle, ImageReader frame pipeline, JSON RPC dispatcher & containment. */
@android.annotation.SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "BlockedPrivateApi", "SoonBlockedPrivateApi", "WrongConstant"})
final class DisplayRuntime {
    private static final String SHELL_PACKAGE = DisplayInputHelper.SHELL_PACKAGE;
    private static final int SHELL_UID = DisplayInputHelper.SHELL_UID;

    private final HandlerThread thread = new HandlerThread("pulse-independent-display");
    private final Handler handler;
    private final HandlerThread inputThread = new HandlerThread("pulse-virtual-input");
    private final Handler inputHandler;
    private final Context context;
    private volatile VirtualDisplay display;
    private ImageReader reader;
    private final HandlerThread frames = new HandlerThread("pulse-root-frames");
    private final Handler frameHandler;
    private final ScheduledExecutorService awakeTimer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "pulse-display-awake");
        t.setDaemon(true);
        return t;
    });
    private ScheduledFuture<?> awakeTask;
    private final Object frameLock = new Object();
    private final Object displayOpLock = new Object();
    private volatile long displayCreateEpoch = 0L;
    private Bitmap latestFrame;
    private Bitmap previewBuffer;
    private final Paint bitmapPaint = new Paint(Paint.FILTER_BITMAP_FLAG);
    private int frameRotation;
    private volatile long frameSequence;
    private boolean framePending = false;
    private long lastFrameProcessedAt = 0L;
    private static final long FRAME_INTERVAL_ACTIVE_MS = 45L; // ~22fps while user finger touches PiP
    private static final long FRAME_INTERVAL_IDLE_MS = 180L;  // ~5.5fps lightweight buffer copy when not touching
    private static final long FRAME_INTERVAL_DEEP_IDLE_MS = 420L; // ~2.4fps when static & no recent touch/preview
    private volatile int previewShortSide = 460;
    private volatile long liveTouchDownTime = 0L;
    private volatile long lastLiveTouchAt = 0L;
    private volatile long lastInteractionAtMs = 0L;
    private volatile long lastPreviewRequestedAtMs = 0L;
    private volatile boolean skippedStaleFrames = false;

    private volatile DisplayFrameCodec.PreviewSnapshot latestPreview = null;

    private volatile boolean trustedDisplay = true;
    private volatile boolean releasedToBackground = false;
    private RootClipboard clipboard;
    private volatile int width = 1472, height = 3232, densityDpi = 480;
    private volatile String activeTier = "compact";

    // Sandbox task containment & main-display anti-jump state
    private final java.util.Set<String> managedVirtualPackages = java.util.Collections.synchronizedSet(new java.util.HashSet<>());
    private final DisplayTaskContainment containment = new DisplayTaskContainment();

    private final Runnable processFrameRunnable = new Runnable() {
        @Override
        public void run() {
            framePending = false;
            lastFrameProcessedAt = SystemClock.uptimeMillis();
            processLatestFrame();
        }
    };

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
        frames.start();
        frameHandler = new Handler(frames.getLooper());
        clipboard = new RootClipboard(context);
    }

    String execute(String command) {
        // Fast lock-free path for preview, status, and live touch stream so frame polling & dragging NEVER queue behind RPCs
        try {
            JSONObject fastCmd = new JSONObject(command);
            String fastOp = fastCmd.optString("op", "");
            if ("preview".equals(fastOp)) {
                return fastPreview(fastCmd).toString();
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
            return task.get(18, TimeUnit.SECONDS);
        } catch (Exception e) {
            return "{\"error\":\"虚拟屏操作超时\"}";
        }
    }

    private JSONObject fastPreview(JSONObject cmd) throws Exception {
        VirtualDisplay vd = this.display;
        if (vd == null || vd.getDisplay() == null || vd.getDisplay().getDisplayId() <= 0) {
            return state().put("hasFrame", false);
        }
        lastPreviewRequestedAtMs = SystemClock.uptimeMillis();
        if (cmd.optBoolean("fresh", false) || skippedStaleFrames) {
            skippedStaleFrames = false;
            processLatestFrame();
        }
        long since = cmd.optLong("since", -1);
        long curSeq = this.frameSequence;
        DisplayFrameCodec.PreviewSnapshot snap = this.latestPreview;
        if (since >= 0 && curSeq > 0 && curSeq == since && snap != null && snap.sequence == curSeq) {
            return state().put("hasFrame", true).put("unchanged", true).put("frameSequence", curSeq);
        }
        if (snap == null || snap.sequence != curSeq) {
            snap = encodeLatestPreviewOnDemand();
        }
        if (snap == null) {
            return state().put("hasFrame", false).put("frameSequence", 0);
        }
        if (since >= 0 && snap.sequence == since) {
            return state().put("hasFrame", true).put("unchanged", true).put("frameSequence", snap.sequence);
        }
        return state()
            .put("hasFrame", true)
            .put("width", snap.width)
            .put("height", snap.height)
            .put("image_width", snap.imageWidth)
            .put("image_height", snap.imageHeight)
            .put("rotation", snap.rotation)
            .put("frameSequence", snap.sequence)
            .put("frameHash", snap.frameHash)
            .put("image", snap.base64);
    }

    private DisplayFrameCodec.PreviewSnapshot encodeLatestPreviewOnDemand() {
        synchronized (frameLock) {
            if (latestFrame == null || display == null) return latestPreview;
            long seq = frameSequence;
            if (latestPreview != null && latestPreview.sequence == seq) return latestPreview;
            Bitmap[] outBuf = new Bitmap[]{previewBuffer};
            DisplayFrameCodec.PreviewSnapshot snap = DisplayFrameCodec.encodePreview(
                latestFrame, seq, this.width, this.height, frameRotation,
                previewShortSide, previewBuffer, bitmapPaint, outBuf
            );
            previewBuffer = outBuf[0];
            this.latestPreview = snap;
            return snap;
        }
    }

    private JSONObject fastTouch(JSONObject cmd) throws Exception {
        VirtualDisplay vd = this.display;
        if (vd == null || vd.getDisplay() == null || vd.getDisplay().getDisplayId() <= 0 || inputHandler == null) {
            return state().put("ok", false);
        }
        releasedToBackground = false;
        final int displayId = vd.getDisplay().getDisplayId();
        final int rot = vd.getDisplay().getRotation();
        final int lw = (rot % 2 == 0) ? width : height;
        final int lh = (rot % 2 == 0) ? height : width;
        final String actionStr = cmd.optString("action", "move").trim().toLowerCase();
        final float x = (float) Math.max(0, Math.min(Math.max(1, lw) - 1, cmd.optInt("x", 0)));
        final float y = (float) Math.max(0, Math.min(Math.max(1, lh) - 1, cmd.optInt("y", 0)));
        boolean posted = inputHandler.post(() -> {
            try {
                long now = SystemClock.uptimeMillis();
                lastLiveTouchAt = now;
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

    private void onFrameAvailable(ImageReader source) {
        if (releasedToBackground || display == null) {
            try (Image img = source.acquireLatestImage()) {} catch (Throwable ignored) {}
            return;
        }
        long now = SystemClock.uptimeMillis();
        if (latestFrame != null && (now - lastPreviewRequestedAtMs > 4000L) && (now - lastInteractionAtMs > 4000L)) {
            try (Image img = source.acquireLatestImage()) {} catch (Throwable ignored) {}
            skippedStaleFrames = true;
            return;
        }
        scheduleFrameProcess();
    }

    private void scheduleFrameProcess() {
        if (framePending || display == null || releasedToBackground) return;
        framePending = true;
        long now = SystemClock.uptimeMillis();
        long recentMs = Math.max(lastInteractionAtMs, lastPreviewRequestedAtMs);
        long interval;
        if (now - lastLiveTouchAt < 1200L) {
            interval = FRAME_INTERVAL_ACTIVE_MS;
        } else if (now - recentMs < 5000L) {
            interval = FRAME_INTERVAL_IDLE_MS;
        } else {
            interval = FRAME_INTERVAL_DEEP_IDLE_MS;
        }
        long wait = lastFrameProcessedAt + interval - now;
        if (wait > 0) {
            frameHandler.postDelayed(processFrameRunnable, wait);
        } else {
            frameHandler.post(processFrameRunnable);
        }
    }

    private void processLatestFrame() {
        ImageReader src = this.reader;
        VirtualDisplay vd = this.display;
        if (src == null || vd == null) return;
        try (Image image = src.acquireLatestImage()) {
            if (image == null || releasedToBackground) return;
            int rot = vd.getDisplay().getRotation();
            int bufH = this.height;
            synchronized (frameLock) {
                if (this.reader != src || this.display == null) return;
                Image.Plane plane = image.getPlanes()[0];
                int padded = plane.getRowStride() / plane.getPixelStride();
                if (latestFrame == null || latestFrame.getWidth() != padded || latestFrame.getHeight() != bufH) {
                    if (latestFrame != null) latestFrame.recycle();
                    latestFrame = Bitmap.createBitmap(padded, bufH, Bitmap.Config.ARGB_8888);
                }
                latestFrame.copyPixelsFromBuffer(plane.getBuffer().rewind());
                frameRotation = rot;
                frameSequence++;
            }
        } catch (Throwable ignored) {}
    }

    @android.annotation.SuppressLint("WrongConstant")
    private JSONObject run(JSONObject cmd) throws Exception {
        String op = cmd.getString("op");
        if (op.equals("create")) {
            int myUid = android.os.Process.myUid();
            if (myUid != 0 && myUid != 2000) throw new Exception("虚拟屏需要 Root (uid 0) 或 Shell 权限");
            String requestedTier = cmd.optString("tier", activeTier).trim().toLowerCase();
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

                if (display != null && display.getDisplay() != null && display.getDisplay().getDisplayId() > 0 && !cmd.optBoolean("recreate", false)) {
                    releasedToBackground = false;
                    activeTier = requestedTier;
                    previewShortSide = cmd.optInt("preview_short_side", spec[3]);
                    int existingId = display.getDisplay().getDisplayId();
                    if (wantW != width || wantH != height || wantDpi != densityDpi) {
                        // Never resize an existing VirtualDisplay in-place via setSurface/resize;
                        // release & recreate cleanly so SurfaceFlinger/ImageReader buffers stay healthy.
                        release(true);
                    } else {
                        DisplayInputHelper.ensureAwakeAndUnlocked(existingId);
                        startKeepAwake(existingId);
                        String curTop = DisplayInputHelper.topPackageOnDisplay(existingId);
                        if (curTop == null || curTop.isEmpty()
                            || (!cmd.optBoolean("skip_home", false) && "com.android.launcher".equals(curTop))) {
                            try {
                                DisplayInputHelper.launchOnDisplay(existingId, "dev.bubuki.proactive/.DisplayTestActivity", 0);
                            } catch (Throwable ignored) {}
                        }
                        if (reader != null) {
                            reader.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
                            frameHandler.post(this::scheduleFrameProcess);
                        }
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

                reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
                Surface surface = reader.getSurface();
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
                    try {
                        trustedDisplay = tCfg[0];
                        int flags = DisplayInputHelper.displayFlags(
                            Build.VERSION.SDK_INT, tCfg[0], tCfg[1], tCfg[2], tCfg[3]
                        );
                        display = dm.createVirtualDisplay("Pulse-Agent", width, height, densityDpi, surface, flags);
                        if (display != null && display.getDisplay() != null && display.getDisplay().getDisplayId() > 0) {
                            break;
                        }
                    } catch (Throwable err) {
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
            String initialTop = DisplayInputHelper.topPackageOnDisplay(displayId);
            if (!cmd.optBoolean("skip_home", false) || initialTop == null || initialTop.isEmpty()) {
                try {
                    DisplayInputHelper.launchOnDisplay(displayId, "dev.bubuki.proactive/.DisplayTestActivity", 0);
                } catch (Throwable ignored) {}
            }

            reader.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
            frameHandler.post(this::scheduleFrameProcess);
            return state().put("newly_created", true).put("reused_display", false);
            }
        } else if (op.equals("resize")) {
            requireDisplay();
            Constructor<DisplayManager> ctor = DisplayManager.class.getDeclaredConstructor(Context.class);
            ctor.setAccessible(true);
            DisplayManager dm = ctor.newInstance(context);
            String requestedTier = cmd.optString("tier", activeTier).trim().toLowerCase();
            if (requestedTier.isEmpty()) requestedTier = "compact";
            int[] spec = DisplayInputHelper.computeAdaptiveDisplaySpec(dm, requestedTier);
            activeTier = requestedTier;
            previewShortSide = cmd.optInt("preview_short_side", spec[3]);
            latestPreview = null;
            if (cmd.optBoolean("force_display_resize", false)) {
                int newW = cmd.optInt("width", spec[0]);
                int newH = cmd.optInt("height", spec[1]);
                int newDpi = cmd.optInt("dpi", spec[2]);
                synchronized (frameLock) {
                    if (newW != width || newH != height) {
                        width = newW;
                        height = newH;
                        ImageReader oldReader = this.reader;
                        this.reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
                        this.reader.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
                        display.setSurface(this.reader.getSurface());
                        if (oldReader != null) {
                            try { oldReader.setOnImageAvailableListener(null, null); oldReader.close(); } catch (Throwable ignored) {}
                        }
                        if (latestFrame != null) {
                            latestFrame.recycle();
                            latestFrame = null;
                        }
                    }
                    densityDpi = newDpi;
                    display.resize(width, height, densityDpi);
                }
                DisplayInputHelper.setFullscreenWindowingMode(display.getDisplay().getDisplayId(), width, height, densityDpi);
            }
            scheduleFrameProcess();
            return state().put("resized", true).put("tier", activeTier);
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
            return fastPreview(cmd);
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
            requireDisplay();
            lastPreviewRequestedAtMs = SystemClock.uptimeMillis();
            skippedStaleFrames = false;
            processLatestFrame();
            Bitmap upright = null;
            long sequence = 0;
            for (int i = 0; i < 40 && upright == null; i++) {
                synchronized (frameLock) {
                    if (latestFrame != null) {
                        upright = DisplayFrameCodec.renderUprightBitmap(latestFrame, width, height, frameRotation);
                        sequence = frameSequence;
                    }
                }
                if (upright == null) Thread.sleep(50);
            }
            if (upright == null) throw new Exception("尚未收到工作区画面，先在虚拟屏打开一个应用");
            int logicalW = upright.getWidth();
            int logicalH = upright.getHeight();
            Bitmap encodeBmp = upright;
            try {
                int minDim = Math.min(logicalW, logicalH);
                if (minDim > 720) {
                    float capScale = 720f / (float) minDim;
                    int encW = Math.max(1, Math.round(logicalW * capScale));
                    int encH = Math.max(1, Math.round(logicalH * capScale));
                    encodeBmp = Bitmap.createScaledBitmap(upright, encW, encH, true);
                }
                if (cmd.optBoolean("grid", false)) {
                    DisplayInputHelper.drawAiGridOverlay(encodeBmp);
                }
                ByteArrayOutputStream out = new ByteArrayOutputStream(65536);
                encodeBmp.compress(Bitmap.CompressFormat.JPEG, 84, out);
                return state().put("width", logicalW).put("height", logicalH)
                    .put("image_width", encodeBmp.getWidth())
                    .put("image_height", encodeBmp.getHeight())
                    .put("frameSequence", sequence)
                    .put("image", Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP));
            } finally {
                if (encodeBmp != null && encodeBmp != upright) {
                    encodeBmp.recycle();
                }
                upright.recycle();
            }
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
            String key = cmd.optString("key", "").trim().toLowerCase();
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
            return state().put("opened_uri", uri).put("output", out);
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
                String p = cmd.optString("policy", "local").trim().toLowerCase();
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
            return state().put("output", output);
        } else if (op.equals("ui_tree")) {
            int id = requireDisplay();
            int rot = display.getDisplay().getRotation();
            int lw = rot % 2 == 0 ? width : height;
            int lh = rot % 2 == 0 ? height : width;
            return VirtualDisplayUiTreeHelper.dumpDisplay(id, lw, lh);
        } else if (!op.equals("status")) {
            throw new Exception("未知虚拟屏操作");
        }
        return state();
    }

    private void onBeforeVirtualInteraction(int displayId) {
        lastInteractionAtMs = SystemClock.uptimeMillis();
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
        lastInteractionAtMs = SystemClock.uptimeMillis();
        awakeTask = awakeTimer.scheduleWithFixedDelay(() -> {
            try {
                if (SystemClock.uptimeMillis() - lastInteractionAtMs > 45_000L) {
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

    private void stopKeepAwake() {
        if (awakeTask != null) {
            awakeTask.cancel(false);
            awakeTask = null;
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

    private String release() {
        return release(false);
    }

    private String release(boolean destroy) {
        synchronized (displayOpLock) {
            if (destroy) {
                displayCreateEpoch++;
            }
            stopKeepAwake();
            inputHandler.removeCallbacksAndMessages(null);
            frameHandler.removeCallbacksAndMessages(null);
            framePending = false;
            if (!destroy && display != null) {
                releasedToBackground = true;
                return "";
            }
            releasedToBackground = false;
            latestPreview = null;
            containment.lastPromotedTaskId = -1;
            containment.lastPromotedPkg = "";
            containment.preActionMainTaskIds.clear();
            if (display != null) {
                VirtualDisplay vd = display;
                display = null;
                DisplayTaskContainment.releaseTasksAndDisplay(vd, managedVirtualPackages);
            }
            synchronized (frameLock) {
                if (reader != null) {
                    try { reader.setOnImageAvailableListener(null, null); reader.close(); } catch (Throwable ignored) {}
                    reader = null;
                }
                if (latestFrame != null) {
                    latestFrame.recycle();
                    latestFrame = null;
                }
                if (previewBuffer != null) {
                    previewBuffer.recycle();
                    previewBuffer = null;
                }
                frameSequence = 0;
            }
            return "";
        }
    }

    void close() {
        if (display != null) {
            execute("{\"op\":\"release\",\"destroy\":true}");
        }
        awakeTimer.shutdownNow();
        inputThread.quitSafely();
        thread.quitSafely();
        frames.quitSafely();
    }

    static Context systemContext() throws Exception {
        return DisplayInputHelper.systemContext();
    }
}
