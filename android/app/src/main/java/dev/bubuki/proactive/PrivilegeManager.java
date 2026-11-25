package dev.bubuki.proactive;

import android.app.NotificationManager;
import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.provider.Settings;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.*;

/**
 * Privileged transport supporting Root (`su`, uid 0), non-root wireless Shizuku (`UserService`,
 * uid 2000/0), and built-in non-root Wireless ADB (`uid 2000`) over direct pipes, avoiding SELinux
 * abstract socket blocks and Binder size limits.
 */
final class PrivilegeManager {
    private static final String RPC_PREFIX = "PULSE_RPC:";
    private final Context context;
    private volatile boolean rootGranted = false;
    private volatile boolean daemonReady = false;
    private volatile String privilegeMode = "none";
    private volatile int grantedUid = -1;
    private volatile int suMode = 0;
    private volatile String lastDiag = "";
    private BufferedReader rootIn;
    private BufferedWriter rootOut;
    private volatile InputStream rootRawIn;
    private volatile OutputStream rootRawOut;
    private java.lang.Process rootProcess;
    private io.github.muntashirakon.adb.AdbStream adbStream;
    private ShizukuController.PipeSession shizukuPipe;
    private volatile Thread probeOwnerThread = null;
    private volatile long probeStartedAtUptimeMs = 0L;
    private final java.util.concurrent.locks.ReentrantLock probeLock = new java.util.concurrent.locks.ReentrantLock();
    private final java.util.concurrent.locks.ReentrantLock pipeLock = new java.util.concurrent.locks.ReentrantLock();
    private final Object probeCoalesceLock = new Object();
    private CompletableFuture<JSONObject> activeProbeFuture;
    private volatile long lastA11yRebindUptime = 0L;
    private final java.util.concurrent.atomic.AtomicLong reqSeq = new java.util.concurrent.atomic.AtomicLong(1L);
    private final ConcurrentHashMap<String, CompletableFuture<String>> pendingRpcs = new ConcurrentHashMap<>();
    private volatile CompletableFuture<String> handshakeFuture;
    private volatile Runnable onDaemonLostListener;
    private final ExecutorService ioPool = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pulse-root-io");
        t.setDaemon(true);
        return t;
    });

    void setOnDaemonLostListener(Runnable listener) {
        this.onDaemonLostListener = listener;
    }

    private void notifyDaemonLost() {
        Runnable l = onDaemonLostListener;
        if (l != null && !ioPool.isShutdown()) {
            try {
                ioPool.submit(() -> {
                    try { l.run(); } catch (Throwable ignored) {}
                });
            } catch (RejectedExecutionException ignored) {}
        }
    }

    PrivilegeManager(Context context) {
        this.context = context.getApplicationContext();
        this.rootGranted = false;
        this.daemonReady = false;
        this.privilegeMode = "none";
        this.grantedUid = -1;
        try {
            JSONObject saved = SecurePrefs.read(this.context);
            this.suMode = saved.optInt("suModeHint", 0);
        } catch (Throwable ignored) {}
        try {
            ShizukuController.get(this.context).setOnBinderDeadListener(() -> {
                if ("shizuku".equals(privilegeMode)) {
                    forceResetDaemon();
                }
            });
        } catch (Throwable ignored) {}
    }

    boolean isRootGrantedFast() {
        return rootGranted && daemonReady && grantedUid == 0;
    }

    boolean isPrivilegedReady() {
        if (!daemonReady) return false;
        if ("root".equals(privilegeMode) && rootGranted && grantedUid == 0) {
            return rootProcess != null && rootProcess.isAlive() && rootOut != null;
        }
        if ("shizuku".equals(privilegeMode) && (grantedUid == 0 || grantedUid == 2000)) {
            return shizukuPipe != null && !shizukuPipe.isClosed()
                && ShizukuController.get(context).isServiceAlive() && rootOut != null;
        }
        if ("wireless_adb".equals(privilegeMode) && grantedUid == 2000) {
            return adbStream != null && !adbStream.isClosed() && rootOut != null;
        }
        return false;
    }

    String getPrivilegeMode() {
        return privilegeMode;
    }

    int getGrantedUid() {
        return grantedUid;
    }

    JSONObject status() throws Exception {
        String listener = Settings.Secure.getString(context.getContentResolver(), "enabled_notification_listeners");
        WirelessAdbController.State adbState = WirelessAdbController.get(context).getState();
        ShizukuController.State shizukuState = ShizukuController.get(context).getState();
        boolean privReady = isPrivilegedReady();
        boolean isRoot = privReady && rootGranted && grantedUid == 0;
        String engine = "none";
        if (privReady) {
            if ("shizuku".equals(privilegeMode)) engine = "shizuku";
            else if ("wireless_adb".equals(privilegeMode)) engine = "wireless_adb";
            else if (isRoot) engine = "root";
        }
        return new JSONObject()
            .put("root", isRoot)
            .put("privileged", privReady)
            .put("engine", engine)
            .put("privilege_mode", privilegeMode)
            .put("uid", grantedUid)
            .put("shizuku_installed", shizukuState.installed)
            .put("shizuku_running", shizukuState.binderAlive)
            .put("shizuku_granted", shizukuState.granted)
            .put("shizuku_uid", shizukuState.serverUid)
            .put("adb_paired", adbState.paired)
            .put("adb_connected", adbState.connected)
            .put("sdk", Build.VERSION.SDK_INT)
            .put("isolatedFocusSupported", Build.VERSION.SDK_INT >= 34)
            .put("overlay", Settings.canDrawOverlays(context))
            .put("notifications", context.getSystemService(NotificationManager.class).areNotificationsEnabled())
            .put("listener", listener != null && listener.contains(context.getPackageName()))
            .put("diag", lastDiag);
    }

    private String readStreamBounded(final InputStream is) {
        if (is == null) return "";
        Future<String> fut = ioPool.submit(() -> {
            try {
                return new String(Io.read(is, 2048), StandardCharsets.UTF_8).trim();
            } catch (Throwable ignored) {
                return "";
            }
        });
        try {
            return fut.get(350, TimeUnit.MILLISECONDS);
        } catch (Throwable ignored) {
            fut.cancel(true);
            try { is.close(); } catch (Throwable ignoredClose) {}
            return "";
        }
    }

    static int[] probeModeOrder(int cachedMode) {
        int[] order = new int[5];
        int idx = 0;
        if (cachedMode >= 0 && cachedMode <= 4) {
            order[idx++] = cachedMode;
        }
        for (int m = 0; m <= 4; m++) {
            if (m != cachedMode) {
                order[idx++] = m;
            }
        }
        return order;
    }

    static void destroyProcessBeforeStreams(java.lang.Process proc, InputStream rawIn, OutputStream rawOut) {
        if (proc != null) {
            try { proc.destroyForcibly(); } catch (Throwable ignored) {}
        }
        if (rawOut != null) {
            try { rawOut.close(); } catch (Throwable ignored) {}
        }
        if (rawIn != null) {
            try { rawIn.close(); } catch (Throwable ignored) {}
        }
    }

    JSONObject probe() throws Exception {
        CompletableFuture<JSONObject> shared = null;
        CompletableFuture<JSONObject> mine = null;
        synchronized (probeCoalesceLock) {
            long startedMs = probeStartedAtUptimeMs;
            if (activeProbeFuture != null && !activeProbeFuture.isDone()) {
                if (startedMs > 0L && (SystemClock.uptimeMillis() - startedMs) >= 18_000L) {
                    Thread stuckThread = probeOwnerThread;
                    activeProbeFuture.completeExceptionally(new TimeoutException("特权探测超时重置"));
                    activeProbeFuture = null;
                    if (stuckThread != null) {
                        try { stuckThread.interrupt(); } catch (Throwable ignored) {}
                    }
                    forceResetDaemon();
                    mine = new CompletableFuture<>();
                    activeProbeFuture = mine;
                } else {
                    shared = activeProbeFuture;
                }
            } else {
                mine = new CompletableFuture<>();
                activeProbeFuture = mine;
            }
        }
        if (shared != null) {
            try {
                return shared.get(18, TimeUnit.SECONDS);
            } catch (ExecutionException ee) {
                Throwable cause = ee.getCause();
                if (cause instanceof Exception) throw (Exception) cause;
                throw new Exception(cause);
            } catch (TimeoutException te) {
                long startedMs = probeStartedAtUptimeMs;
                if (startedMs > 0L && (SystemClock.uptimeMillis() - startedMs) >= 18_000L) {
                    synchronized (probeCoalesceLock) {
                        if (activeProbeFuture == shared) {
                            activeProbeFuture = null;
                        }
                    }
                    Thread stuckThread = probeOwnerThread;
                    if (stuckThread != null) {
                        try { stuckThread.interrupt(); } catch (Throwable ignored) {}
                    }
                    forceResetDaemon();
                }
                lastDiag = "特权探测超时重置中，请 3 秒后重试或切回应用前台";
                return status();
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw new Exception("特权探测被中断", ie);
            }
        }
        try {
            JSONObject res = probeInternal();
            mine.complete(res);
            return res;
        } catch (Throwable t) {
            mine.completeExceptionally(t);
            if (t instanceof Exception) throw (Exception) t;
            throw new Exception(t);
        } finally {
            synchronized (probeCoalesceLock) {
                if (activeProbeFuture == mine) {
                    activeProbeFuture = null;
                }
            }
        }
    }

    private JSONObject probeInternal() throws Exception {
        boolean locked = false;
        try {
            locked = probeLock.tryLock(12, TimeUnit.SECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new Exception("特权探测被中断", ie);
        }
        if (!locked) {
            long startedMs = probeStartedAtUptimeMs;
            if (startedMs > 0L && (SystemClock.uptimeMillis() - startedMs) >= 18_000L) {
                Thread stuckThread = probeOwnerThread;
                if (stuckThread != null) {
                    try { stuckThread.interrupt(); } catch (Throwable ignored) {}
                }
                forceResetDaemon();
            }
            try {
                locked = probeLock.tryLock(3, TimeUnit.SECONDS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw new Exception("特权探测被中断", ie);
            }
            if (!locked) {
                lastDiag = "特权探测超时重置中，请 3 秒后重试或切回应用前台";
                return status();
            }
        }
        probeOwnerThread = Thread.currentThread();
        probeStartedAtUptimeMs = SystemClock.uptimeMillis();
        try {
        if (isPrivilegedReady()) {
            return status();
        }
        if (!isPrivilegedReady() && lastDiag != null && lastDiag.contains("特权探测")) {
            lastDiag = "";
        }
        boolean detected = false;
        int winningMode = 0;
        for (int mode : probeModeOrder(suMode)) {
            java.lang.Process process = null;
            try {
                process = startSu("id -u", mode);
                if (!process.waitFor(1200, TimeUnit.MILLISECONDS)) {
                    process.destroyForcibly();
                    continue;
                }
                String out = readStreamBounded(process.getInputStream());
                String err = readStreamBounded(process.getErrorStream());
                if (containsUidZero(out) || containsUidZero(err)) {
                    detected = true;
                    winningMode = mode;
                    break;
                }
            } catch (IOException ioe) {
                // Skip modes whose su binary is absent without blocking the lock
                continue;
            } catch (Throwable ignored) {
            } finally {
                if (process != null && process.isAlive()) process.destroyForcibly();
            }
        }
        if (detected) {
            privilegeMode = "root";
            grantedUid = 0;
            suMode = winningMode;
            Throwable lastRootHsErr = null;
            for (int hsTry = 0; hsTry < 2; hsTry++) {
                try {
                    if (!pipeLock.tryLock(8, TimeUnit.SECONDS)) {
                        forceResetDaemon();
                        throw new IOException("Root 管道锁超时，已触发重置");
                    }
                    try {
                        closeRootLocked(false);
                        ensureRootLocked();
                    } finally {
                        pipeLock.unlock();
                    }
                    rootGranted = true;
                    persistHint(true, winningMode);
                    final int permMode = winningMode;
                    final boolean needA11yRebind = !PulseAccessibilityService.isConnected();
                    ioPool.submit(() -> grantPermissionsViaRootSync(permMode, needA11yRebind));
                    lastDiag = "ok (root uid=0)";
                    return status();
                } catch (Throwable hsErr) {
                    lastRootHsErr = hsErr;
                    if (hsTry == 0) {
                        if (pipeLock.tryLock(2, TimeUnit.SECONDS)) {
                            try { closeRootLocked(false); } finally { pipeLock.unlock(); }
                        }
                        try { Thread.sleep(150L); } catch (InterruptedException ie) {
                            Thread.currentThread().interrupt();
                            throw new Exception("特权探测被中断", ie);
                        }
                    }
                }
            }
            rootGranted = false;
            daemonReady = false;
            privilegeMode = "none";
            grantedUid = -1;
            String errMsg = lastRootHsErr != null && lastRootHsErr.getMessage() != null
                ? lastRootHsErr.getMessage()
                : (lastRootHsErr != null ? lastRootHsErr.getClass().getSimpleName() : "unknown");
            lastDiag = "Root 守护进程握手失败: " + errMsg;
            return status();
        }
        ShizukuController shizuku = ShizukuController.get(context);
        if (shizuku.isReadyToBind()) {
            privilegeMode = "shizuku";
            int sUid = shizuku.getState().serverUid;
            grantedUid = (sUid == 0 || sUid == 2000) ? sUid : 2000;
            rootGranted = (grantedUid == 0);
            suMode = 0;
            try {
                if (!pipeLock.tryLock(8, TimeUnit.SECONDS)) {
                    forceResetDaemon();
                    throw new IOException("Shizuku 管道锁超时，已触发重置");
                }
                try {
                    closeRootLocked(false);
                    ensureRootLocked();
                } finally {
                    pipeLock.unlock();
                }
                persistHint(true, 0);
                final boolean needA11yRebind = !PulseAccessibilityService.isConnected();
                ioPool.submit(() -> grantPermissionsViaActiveDaemonAsync(needA11yRebind));
                lastDiag = "ok (shizuku uid=" + grantedUid + ")";
                return status();
            } catch (Throwable hsErr) {
                rootGranted = false;
                daemonReady = false;
                privilegeMode = "none";
                grantedUid = -1;
                persistHint(false, 0);
                lastDiag = "Shizuku 守护进程握手失败: " + (hsErr.getMessage() != null ? hsErr.getMessage() : hsErr.getClass().getSimpleName());
            }
        } else if (shizuku.isRunningNeedingPermission()) {
            lastDiag = "检测到 Shizuku 正在运行，请点击「一键授权 Shizuku」允许权限";
        }
        boolean adbReady = false;
        try {
            adbReady = ioPool.submit(() -> WirelessAdbController.get(context).ensureConnected())
                .get(5000, TimeUnit.MILLISECONDS);
        } catch (Throwable ignored) {}
        if (adbReady) {
            rootGranted = false;
            privilegeMode = "wireless_adb";
            grantedUid = 2000;
            suMode = 0;
            try {
                if (!pipeLock.tryLock(8, TimeUnit.SECONDS)) {
                    forceResetDaemon();
                    throw new IOException("无线 ADB 管道锁超时，已触发重置");
                }
                try {
                    closeRootLocked(false);
                    ensureRootLocked();
                } finally {
                    pipeLock.unlock();
                }
                persistHint(true, 0);
                final boolean needA11yRebind = !PulseAccessibilityService.isConnected();
                ioPool.submit(() -> grantPermissionsViaActiveDaemonAsync(needA11yRebind));
                lastDiag = "ok (wireless_adb uid=2000)";
                return status();
            } catch (Throwable hsErr) {
                daemonReady = false;
                privilegeMode = "none";
                grantedUid = -1;
                persistHint(false, 0);
                lastDiag = "无线 ADB 守护进程握手失败: " + (hsErr.getMessage() != null ? hsErr.getMessage() : hsErr.getClass().getSimpleName());
            }
        } else {
            rootGranted = false;
            daemonReady = false;
            privilegeMode = "none";
            grantedUid = -1;
            suMode = 0;
            persistHint(false, 0);
            if (lastDiag == null || lastDiag.isEmpty()) {
                String adbErr = WirelessAdbController.get(context).getState().lastError;
                lastDiag = adbErr != null && !adbErr.isEmpty() ? adbErr : "未检测到 Root、已授权 Shizuku 或已配对无线 ADB";
            }
        }
        return status();
        } finally {
            probeStartedAtUptimeMs = 0L;
            if (probeOwnerThread == Thread.currentThread()) {
                probeOwnerThread = null;
            }
            probeLock.unlock();
        }
    }

    private String buildAutoGrantShellCommand(boolean forceRebindA11y) {
        String pkg = context.getPackageName();
        String svc = pkg + "/" + pkg + ".PulseAccessibilityService";
        String svcShort = pkg + "/.PulseAccessibilityService";
        StringBuilder sb = new StringBuilder();
        sb.append("cmd appops set --uid ").append(pkg).append(" SYSTEM_ALERT_WINDOW allow >/dev/null 2>&1; ");
        sb.append("appops set ").append(pkg).append(" SYSTEM_ALERT_WINDOW allow >/dev/null 2>&1; ");
        sb.append("appops set ").append(pkg).append(" PROJECT_MEDIA allow >/dev/null 2>&1; ");
        sb.append("pm grant ").append(pkg).append(" android.permission.POST_NOTIFICATIONS >/dev/null 2>&1; ");
        sb.append("pm grant ").append(pkg).append(" android.permission.READ_SMS >/dev/null 2>&1; ");
        sb.append("appops set ").append(pkg).append(" READ_SMS allow >/dev/null 2>&1; ");
        sb.append("cmd notification allow_listener ").append(pkg).append("/").append(pkg).append(".AgentNotificationListener >/dev/null 2>&1; ");
        sb.append("cur=$(settings get secure enabled_accessibility_services 2>/dev/null); ");
        sb.append("without=''; old_ifs=\"$IFS\"; IFS=':'; ");
        sb.append("for item in $cur; do ");
        sb.append("case \"$item\" in \"\"|\"null\"|\"").append(svc).append("\"|\"").append(svcShort).append("\") ;; ");
        sb.append("*) if [ -z \"$without\" ]; then without=\"$item\"; else without=\"$without:$item\"; fi ;; ");
        sb.append("esac; done; IFS=\"$old_ifs\"; ");
        sb.append("if [ -z \"$without\" ]; then target=\"").append(svc).append("\"; else target=\"$without:").append(svc).append("\"; fi; ");
        if (forceRebindA11y) {
            sb.append("settings put secure enabled_accessibility_services \"$without\" >/dev/null 2>&1; ");
            sb.append("sleep 0.25; ");
            sb.append("settings put secure enabled_accessibility_services \"$target\" >/dev/null 2>&1; ");
        } else {
            sb.append("case \"$cur\" in *\"").append(svc).append("\"*|*\"").append(svcShort).append("\"*) ;; ")
              .append("*) settings put secure enabled_accessibility_services \"$target\" >/dev/null 2>&1 ;; esac; ");
        }
        sb.append("settings put secure accessibility_enabled 1 >/dev/null 2>&1");
        return sb.toString();
    }

    private void grantPermissionsViaRootSync(int mode, boolean forceRebindA11y) {
        java.lang.Process p = null;
        try {
            String grantCmd = buildAutoGrantShellCommand(forceRebindA11y);
            p = startSu(grantCmd, mode);
            if (mode == 4) {
                OutputStream os = p.getOutputStream();
                os.write("exit\n".getBytes(StandardCharsets.UTF_8));
                os.flush();
            }
            p.waitFor(4, TimeUnit.SECONDS);
        } catch (Throwable ignored) {
        } finally {
            if (p != null && p.isAlive()) p.destroyForcibly();
        }
    }

    private void grantPermissionsViaActiveDaemonAsync(boolean forceRebindA11y) {
        try {
            if (!isPrivilegedReady()) return;
            execute(new JSONObject().put("op", "shell").put("command", buildAutoGrantShellCommand(forceRebindA11y)));
        } catch (Throwable ignored) {}
    }

    void ensureAccessibilityBoundAsync() {
        if (!isPrivilegedReady()) return;
        long now = SystemClock.uptimeMillis();
        if (lastA11yRebindUptime != 0L && now - lastA11yRebindUptime < 30_000L) return;
        lastA11yRebindUptime = now;
        if ("root".equals(privilegeMode)) {
            final int mode = suMode;
            ioPool.submit(() -> grantPermissionsViaRootSync(mode, true));
        } else {
            ioPool.submit(() -> grantPermissionsViaActiveDaemonAsync(true));
        }
    }

    private static boolean containsUidZero(String text) {
        if (text == null || text.isEmpty()) return false;
        for (String line : text.split("\\r?\\n")) {
            String trimmed = line.trim();
            if ("0".equals(trimmed) || trimmed.startsWith("uid=0(")) return true;
        }
        return false;
    }

    private void persistHint(boolean granted, int mode) {
        try {
            SecurePrefs.update(context, saved -> {
                saved.put("rootGrantedHint", granted);
                saved.put("suModeHint", mode);
                saved.put("privilegeModeHint", granted ? privilegeMode : "none");
                saved.put("grantedUidHint", granted ? grantedUid : -1);
            });
        } catch (Throwable ignored) {}
    }

    private java.lang.Process startSu(String command, int mode) throws IOException {
        ProcessBuilder pb;
        switch (mode) {
            case 1:
                pb = new ProcessBuilder("su", "0", "sh", "-c", command);
                break;
            case 2:
                pb = new ProcessBuilder("/system/bin/su", "-c", command);
                break;
            case 3:
                pb = new ProcessBuilder("/system/xbin/su", "-c", command);
                break;
            case 4:
                pb = new ProcessBuilder("su");
                break;
            case 0:
            default:
                pb = new ProcessBuilder("su", "-c", command);
                break;
        }
        java.lang.Process proc = pb.start();
        if (mode == 4) {
            OutputStream os = proc.getOutputStream();
            os.write((command + "\n").getBytes(StandardCharsets.UTF_8));
            os.flush();
            if ("id -u".equals(command)) {
                os.write("exit\n".getBytes(StandardCharsets.UTF_8));
                os.flush();
            }
        }
        return proc;
    }

    JSONObject execute(JSONObject command) throws Exception {
        if (!isPrivilegedReady()) {
            probe();
            if (!isPrivilegedReady()) throw new Exception("请先在此手机页面连接 Root、授权 Shizuku 或配对无线 ADB 调试");
        }
        String op = command.optString("op", "");
        long timeoutMs = "shell".equals(op) ? 30000L : 20000L;
        Exception lastErr = null;
        for (int attempt = 0; attempt < 2; attempt++) {
            String reqId = "r" + reqSeq.getAndIncrement();
            CompletableFuture<String> future = new CompletableFuture<>();
            pendingRpcs.put(reqId, future);
            try {
                JSONObject wireCmd = new JSONObject(command.toString());
                wireCmd.put("_req_id", reqId);
                if (!pipeLock.tryLock(10, TimeUnit.SECONDS)) {
                    forceResetDaemon();
                    throw new IOException("特权工具通道繁忙超时 (" + op + ")，已触发重置");
                }
                try {
                    ensureRootLocked();
                    rootOut.write(wireCmd.toString());
                    rootOut.newLine();
                    rootOut.flush();
                } finally {
                    pipeLock.unlock();
                }
                String line = future.get(timeoutMs, TimeUnit.MILLISECONDS);
                if (line == null) throw new IOException("Root 工具会话已断开" + (lastDiag.isEmpty() ? "" : " (" + lastDiag + ")"));
                JSONObject result = new JSONObject(line);
                result.remove("_req_id");
                if (result.has("error")) throw new Exception(result.getString("error"));
                return result;
            } catch (TimeoutException te) {
                // Single request timeout: do NOT close the shared pipe or interrupt other active requests!
                throw new Exception("等待 Root 工具响应超时 (" + op + ")");
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw e;
            } catch (ExecutionException ee) {
                Throwable cause = ee.getCause();
                if (cause instanceof IOException) {
                    if (pipeLock.tryLock(2, TimeUnit.SECONDS)) {
                        try { closeRootLocked(true); } finally { pipeLock.unlock(); }
                    } else {
                        forceResetDaemon();
                    }
                    lastErr = (IOException) cause;
                } else {
                    throw (cause instanceof Exception) ? (Exception) cause : new Exception(cause);
                }
            } catch (IOException e) {
                if (pipeLock.tryLock(2, TimeUnit.SECONDS)) {
                    try { closeRootLocked(true); } finally { pipeLock.unlock(); }
                } else {
                    forceResetDaemon();
                }
                lastErr = e;
            } finally {
                pendingRpcs.remove(reqId);
            }
        }
        if (pipeLock.tryLock(2, TimeUnit.SECONDS)) {
            try {
                if (rootProcess == null && adbStream == null && shizukuPipe == null) {
                    daemonReady = false;
                    rootGranted = false;
                    privilegeMode = "none";
                    grantedUid = -1;
                }
            } finally {
                pipeLock.unlock();
            }
        }
        throw lastErr != null ? lastErr : new Exception("特权工具调用失败");
    }

    private void ensureRootLocked() throws Exception {
        boolean isShizukuMode = "shizuku".equals(privilegeMode);
        boolean isAdbMode = "wireless_adb".equals(privilegeMode);
        if (isShizukuMode) {
            if (shizukuPipe != null && !shizukuPipe.isClosed()
                && ShizukuController.get(context).isServiceAlive()
                && rootIn != null && rootOut != null) {
                return;
            }
        } else if (isAdbMode) {
            if (adbStream != null && !adbStream.isClosed() && rootIn != null && rootOut != null) return;
        } else {
            if (rootProcess != null && rootProcess.isAlive() && rootIn != null && rootOut != null) return;
        }
        closeRootLocked(false);
        lastDiag = "";
        String token = UUID.randomUUID().toString();
        String apk = context.getApplicationInfo().sourceDir;
        String escapedApk = apk.replace("'", "'\\''");
        String pkg = context.getPackageName();
        String cmd = "export CLASSPATH='" + escapedApk + "'; exec /system/bin/app_process /system/bin --nice-name="
            + pkg + (isAdbMode ? ":adb " : ":root ") + "dev.bubuki.proactive.RootDisplayServer " + token + " " + android.os.Process.myUid();
        InputStream inStream;
        OutputStream outStream;
        java.lang.Process proc = null;
        if (isShizukuMode) {
            ShizukuController.PipeSession session = ioPool.submit(() -> ShizukuController.get(context).openPipeSession(token, 8000L))
                .get(10000, TimeUnit.MILLISECONDS);
            shizukuPipe = session;
            grantedUid = (session.uid == 0 || session.uid == 2000) ? session.uid : 2000;
            rootGranted = (grantedUid == 0);
            inStream = session.in;
            outStream = session.out;
        } else if (isAdbMode) {
            io.github.muntashirakon.adb.AdbStream stream = ioPool.submit(() -> WirelessAdbController.get(context).openShellStream(cmd))
                .get(6000, TimeUnit.MILLISECONDS);
            adbStream = stream;
            inStream = stream.openInputStream();
            outStream = stream.openOutputStream();
        } else {
            proc = startSu(cmd, suMode);
            rootProcess = proc;
            final InputStream errStream = proc.getErrorStream();
            ioPool.submit(() -> {
                try (BufferedReader errReader = new BufferedReader(new InputStreamReader(errStream, StandardCharsets.UTF_8))) {
                    String errLine;
                    StringBuilder sb = new StringBuilder();
                    while ((errLine = errReader.readLine()) != null) {
                        if (sb.length() > 600) sb.delete(0, 300);
                        if (sb.length() > 0) sb.append(" | ");
                        sb.append(errLine.trim());
                        lastDiag = sb.toString();
                    }
                } catch (Throwable ignored) {}
            });
            inStream = proc.getInputStream();
            outStream = proc.getOutputStream();
        }
        rootRawIn = inStream;
        rootRawOut = outStream;
        final BufferedReader reader = new BufferedReader(new InputStreamReader(inStream, StandardCharsets.UTF_8));
        rootIn = reader;
        rootOut = new BufferedWriter(new OutputStreamWriter(outStream, StandardCharsets.UTF_8));
        final java.lang.Process expectedProc = proc;
        final io.github.muntashirakon.adb.AdbStream expectedAdb = adbStream;
        final ShizukuController.PipeSession expectedShizuku = shizukuPipe;
        final CompletableFuture<String> hs = new CompletableFuture<>();
        handshakeFuture = hs;
        ioPool.submit(() -> {
            try {
                String line;
                while ((line = reader.readLine()) != null) {
                    int idx = line.indexOf(RPC_PREFIX);
                    if (idx < 0) continue;
                    String payload = line.substring(idx + RPC_PREFIX.length()).trim();
                    if (payload.startsWith("READY:")) {
                        CompletableFuture<String> h = handshakeFuture;
                        if (h != null && !h.isDone()) h.complete(payload);
                        continue;
                    }
                    String targetReqId = "";
                    try {
                        JSONObject obj = new JSONObject(payload);
                        targetReqId = obj.optString("_req_id", "");
                    } catch (Throwable ignored) {}
                    if (!targetReqId.isEmpty()) {
                        CompletableFuture<String> f = pendingRpcs.remove(targetReqId);
                        if (f != null) f.complete(payload);
                    } else {
                        for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
                            if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                                entry.getValue().complete(payload);
                                break;
                            }
                        }
                    }
                }
            } catch (Throwable ignored) {
            } finally {
                if (!hs.isDone()) hs.completeExceptionally(new IOException("特权管道已关闭"));
                CompletableFuture<String> h = handshakeFuture;
                if (h != null && !h.isDone()) h.completeExceptionally(new IOException("特权管道已关闭"));
                boolean stillOwner = (expectedProc != null && expectedProc == rootProcess)
                    || (expectedAdb != null && expectedAdb == adbStream)
                    || (expectedShizuku != null && expectedShizuku == shizukuPipe);
                if (stillOwner) {
                    boolean wasReady = daemonReady;
                    daemonReady = false;
                    for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
                        if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                            entry.getValue().completeExceptionally(new IOException("特权管道已断开"));
                        }
                    }
                    if (wasReady) notifyDaemonLost();
                }
            }
        });
        String handshake;
        try {
            handshake = hs.get(8000, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            String diag = lastDiag;
            if ("ok".equalsIgnoreCase(diag)) diag = "";
            String exitInfo = "";
            try {
                if (proc != null && !proc.isAlive()) exitInfo = "exit=" + proc.exitValue();
            } catch (Throwable ignored) {}
            closeRootLocked(false);
            String detail = !diag.isEmpty() ? diag : (!exitInfo.isEmpty() ? exitInfo : "daemon_handshake_timeout");
            throw new IOException("特权工具进程握手未完成 (" + detail + ")");
        }
        if (!("READY:" + token).equals(handshake)) {
            String diag = lastDiag;
            if ("ok".equalsIgnoreCase(diag)) diag = "";
            closeRootLocked(false);
            throw new IOException("特权工具进程握手未完成" + (diag.isEmpty() ? " (invalid_token)" : " (" + diag + ")"));
        }
        daemonReady = true;
    }

    private void closeRootLocked(boolean notifyLost) {
        boolean wasReady = daemonReady;
        final BufferedWriter prevOut = rootOut;
        if (wasReady && prevOut != null) {
            // Best-effort graceful release before tearing down daemon so VirtualDisplay never leaks
            try {
                prevOut.write("{\"op\":\"release\",\"destroy\":true}");
                prevOut.newLine();
                prevOut.flush();
            } catch (Throwable ignored) {}
        }
        daemonReady = false;
        final java.lang.Process staleProc = rootProcess;
        final io.github.muntashirakon.adb.AdbStream staleAdb = adbStream;
        final ShizukuController.PipeSession staleShizuku = shizukuPipe;
        final InputStream staleRawIn = rootRawIn;
        final OutputStream staleRawOut = rootRawOut;
        final CompletableFuture<String> hs = handshakeFuture;
        rootProcess = null;
        adbStream = null;
        shizukuPipe = null;
        rootRawIn = null;
        rootRawOut = null;
        rootIn = null;
        rootOut = null;
        handshakeFuture = null;
        if (hs != null && !hs.isDone()) {
            hs.completeExceptionally(new IOException("特权管道已关闭"));
        }
        for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
            if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                entry.getValue().completeExceptionally(new IOException("特权管道已关闭"));
            }
        }
        if (staleShizuku != null) {
            try { staleShizuku.close(); } catch (Exception ignored) {}
        }
        if (staleAdb != null) {
            try { staleAdb.close(); } catch (Exception ignored) {}
        }
        destroyProcessBeforeStreams(staleProc, staleRawIn, staleRawOut);
        if (notifyLost && wasReady) notifyDaemonLost();
    }

    void forceResetDaemon() {
        boolean wasReady = daemonReady;
        daemonReady = false;
        final java.lang.Process staleProc = rootProcess;
        final io.github.muntashirakon.adb.AdbStream staleAdb = adbStream;
        final ShizukuController.PipeSession staleShizuku = shizukuPipe;
        final InputStream staleRawIn = rootRawIn;
        final OutputStream staleRawOut = rootRawOut;
        final CompletableFuture<String> hs = handshakeFuture;
        rootProcess = null;
        adbStream = null;
        shizukuPipe = null;
        rootRawIn = null;
        rootRawOut = null;
        rootIn = null;
        rootOut = null;
        handshakeFuture = null;
        if (hs != null && !hs.isDone()) {
            hs.completeExceptionally(new IOException("daemon_force_reset"));
        }
        for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
            if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                entry.getValue().completeExceptionally(new IOException("daemon_force_reset"));
            }
        }
        if (!ioPool.isShutdown()) {
            try {
                ioPool.submit(() -> {
                    if (staleShizuku != null) {
                        try { staleShizuku.close(); } catch (Throwable ignored) {}
                    }
                    if (staleAdb != null) {
                        try { staleAdb.close(); } catch (Throwable ignored) {}
                    }
                    destroyProcessBeforeStreams(staleProc, staleRawIn, staleRawOut);
                });
            } catch (RejectedExecutionException ignored) {
                if (staleShizuku != null) {
                    try { staleShizuku.close(); } catch (Throwable ignoredClose) {}
                }
                if (staleAdb != null) {
                    try { staleAdb.close(); } catch (Throwable ignoredClose) {}
                }
                destroyProcessBeforeStreams(staleProc, staleRawIn, staleRawOut);
            }
        } else {
            if (staleShizuku != null) {
                try { staleShizuku.close(); } catch (Throwable ignored) {}
            }
            if (staleAdb != null) {
                try { staleAdb.close(); } catch (Throwable ignored) {}
            }
            destroyProcessBeforeStreams(staleProc, staleRawIn, staleRawOut);
        }
        if (wasReady) notifyDaemonLost();
    }

    void close() {
        try {
            if (!pipeLock.tryLock(2, TimeUnit.SECONDS)) {
                forceResetDaemon();
                return;
            }
            try {
                try {
                    if (rootOut != null) {
                        rootOut.write("{\"op\":\"exit\"}\n");
                        rootOut.flush();
                    }
                } catch (IOException ignored) {}
                if ("shizuku".equals(privilegeMode)) {
                    try { ShizukuController.get(context).unbindService(true); } catch (Throwable ignored) {}
                }
                closeRootLocked(false);
            } finally {
                pipeLock.unlock();
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            forceResetDaemon();
        } finally {
            ioPool.shutdownNow();
        }
    }
}
