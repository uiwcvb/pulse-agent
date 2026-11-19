package dev.bubuki.proactive;

import android.app.NotificationManager;
import android.content.Context;
import android.os.Build;
import android.provider.Settings;
import org.json.JSONObject;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.*;

/**
 * Privileged transport supporting both Root (`su`, uid 0) and non-root Wireless ADB (`uid 2000`)
 * over direct pipes to `RootDisplayServer` (`app_process`), avoiding SELinux abstract socket blocks.
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
    private java.lang.Process rootProcess;
    private io.github.muntashirakon.adb.AdbStream adbStream;
    private final java.util.concurrent.locks.ReentrantLock probeLock = new java.util.concurrent.locks.ReentrantLock();
    private final java.util.concurrent.locks.ReentrantLock pipeLock = new java.util.concurrent.locks.ReentrantLock();
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
        if (l != null) {
            ioPool.submit(() -> {
                try { l.run(); } catch (Throwable ignored) {}
            });
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
    }

    boolean isRootGrantedFast() {
        return rootGranted && daemonReady && grantedUid == 0;
    }

    boolean isPrivilegedReady() {
        if (!daemonReady) return false;
        if ("root".equals(privilegeMode) && rootGranted && grantedUid == 0) {
            return rootProcess != null && rootProcess.isAlive() && rootOut != null;
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
        boolean privReady = isPrivilegedReady();
        boolean isRoot = privReady && rootGranted && grantedUid == 0;
        return new JSONObject()
            .put("root", isRoot)
            .put("privileged", privReady)
            .put("engine", "wireless_adb".equals(privilegeMode) ? "wireless_adb" : "root")
            .put("privilege_mode", privilegeMode)
            .put("uid", grantedUid)
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
            return fut.get(800, TimeUnit.MILLISECONDS);
        } catch (Throwable ignored) {
            fut.cancel(true);
            try { is.close(); } catch (Throwable ignoredClose) {}
            return "";
        }
    }

    JSONObject probe() throws Exception {
        boolean locked = false;
        try {
            locked = probeLock.tryLock(8, TimeUnit.SECONDS);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new Exception("特权探测被中断", ie);
        }
        if (!locked) {
            forceResetDaemon();
            try {
                locked = probeLock.tryLock(2, TimeUnit.SECONDS);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw new Exception("特权探测被中断", ie);
            }
            if (!locked) {
                lastDiag = "特权探测正忙，已触发重置守护进程";
                return status();
            }
        }
        try {
        boolean detected = false;
        int winningMode = 0;
        for (int mode = 0; mode <= 4; mode++) {
            java.lang.Process process = null;
            try {
                process = startSu("id -u", mode);
                if (!process.waitFor(2500, TimeUnit.MILLISECONDS)) {
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
            } catch (Throwable ignored) {
            } finally {
                if (process != null && process.isAlive()) process.destroyForcibly();
            }
        }
        if (detected) {
            privilegeMode = "root";
            grantedUid = 0;
            suMode = winningMode;
            try {
                if (!pipeLock.tryLock(10, TimeUnit.SECONDS)) {
                    forceResetDaemon();
                    throw new IOException("Root 管道锁超时，已触发重置");
                }
                try {
                    closeRootLocked();
                    ensureRootLocked();
                } finally {
                    pipeLock.unlock();
                }
                rootGranted = true;
                persistHint(true, winningMode);
                final int permMode = winningMode;
                ioPool.submit(() -> grantPermissionsViaRootSync(permMode));
                lastDiag = "ok (root uid=0)";
                return status();
            } catch (Throwable hsErr) {
                rootGranted = false;
                daemonReady = false;
                privilegeMode = "none";
                grantedUid = -1;
                lastDiag = "Root 守护进程握手失败: " + (hsErr.getMessage() != null ? hsErr.getMessage() : hsErr.getClass().getSimpleName());
            }
        }
        boolean adbReady = false;
        try {
            adbReady = ioPool.submit(() -> WirelessAdbController.get(context).ensureConnected())
                .get(6000, TimeUnit.MILLISECONDS);
        } catch (Throwable ignored) {}
        if (adbReady) {
            rootGranted = false;
            privilegeMode = "wireless_adb";
            grantedUid = 2000;
            suMode = 0;
            try {
                if (!pipeLock.tryLock(10, TimeUnit.SECONDS)) {
                    forceResetDaemon();
                    throw new IOException("无线 ADB 管道锁超时，已触发重置");
                }
                try {
                    closeRootLocked();
                    ensureRootLocked();
                } finally {
                    pipeLock.unlock();
                }
                persistHint(true, 0);
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
                lastDiag = adbErr != null && !adbErr.isEmpty() ? adbErr : "未检测到 Root 或已配对无线 ADB";
            }
        }
        return status();
        } finally {
            probeLock.unlock();
        }
    }

    private String buildAutoGrantShellCommand() {
        String pkg = context.getPackageName();
        String svc = pkg + "/" + pkg + ".PulseAccessibilityService";
        return "cmd appops set --uid " + pkg + " SYSTEM_ALERT_WINDOW allow >/dev/null 2>&1; "
            + "appops set " + pkg + " SYSTEM_ALERT_WINDOW allow >/dev/null 2>&1; "
            + "appops set " + pkg + " PROJECT_MEDIA allow >/dev/null 2>&1; "
            + "pm grant " + pkg + " android.permission.POST_NOTIFICATIONS >/dev/null 2>&1; "
            + "cur=$(settings get secure enabled_accessibility_services 2>/dev/null); "
            + "case \"$cur\" in *\"" + svc + "\"*) ;; \"\"|\"null\") settings put secure enabled_accessibility_services \"" + svc + "\" ;; *) settings put secure enabled_accessibility_services \"$cur:" + svc + "\" ;; esac; "
            + "settings put secure accessibility_enabled 1 >/dev/null 2>&1";
    }

    private void grantPermissionsViaRootSync(int mode) {
        java.lang.Process p = null;
        try {
            String grantCmd = buildAutoGrantShellCommand();
            p = startSu(grantCmd, mode);
            if (mode == 4) {
                OutputStream os = p.getOutputStream();
                os.write("exit\n".getBytes(StandardCharsets.UTF_8));
                os.flush();
            }
            p.waitFor(3, TimeUnit.SECONDS);
        } catch (Throwable ignored) {
        } finally {
            if (p != null && p.isAlive()) p.destroyForcibly();
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
            synchronized (SecurePrefs.class) {
                JSONObject saved = SecurePrefs.read(context);
                saved.put("rootGrantedHint", granted);
                saved.put("suModeHint", mode);
                saved.put("privilegeModeHint", granted ? privilegeMode : "none");
                saved.put("grantedUidHint", granted ? grantedUid : -1);
                SecurePrefs.save(context, saved);
            }
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
            if (!isPrivilegedReady()) throw new Exception("请先在此手机页面连接 Root 或配对无线 ADB 调试");
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
                        try { closeRootLocked(); } finally { pipeLock.unlock(); }
                    } else {
                        forceResetDaemon();
                    }
                    lastErr = (IOException) cause;
                } else {
                    throw (cause instanceof Exception) ? (Exception) cause : new Exception(cause);
                }
            } catch (IOException e) {
                if (pipeLock.tryLock(2, TimeUnit.SECONDS)) {
                    try { closeRootLocked(); } finally { pipeLock.unlock(); }
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
                if (rootProcess == null && adbStream == null) {
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
        boolean isAdbMode = "wireless_adb".equals(privilegeMode);
        if (isAdbMode) {
            if (adbStream != null && !adbStream.isClosed() && rootIn != null && rootOut != null) return;
        } else {
            if (rootProcess != null && rootProcess.isAlive() && rootIn != null && rootOut != null) return;
        }
        closeRootLocked();
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
        if (isAdbMode) {
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
        final BufferedReader reader = new BufferedReader(new InputStreamReader(inStream, StandardCharsets.UTF_8));
        rootIn = reader;
        rootOut = new BufferedWriter(new OutputStreamWriter(outStream, StandardCharsets.UTF_8));
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
                CompletableFuture<String> h = handshakeFuture;
                if (h != null && !h.isDone()) h.completeExceptionally(new IOException("特权管道已关闭"));
                for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
                    if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                        entry.getValue().completeExceptionally(new IOException("特权管道已断开"));
                    }
                }
            }
        });
        String handshake;
        try {
            handshake = hs.get(12000, TimeUnit.MILLISECONDS);
        } catch (Exception e) {
            String diag = lastDiag;
            if ("ok".equalsIgnoreCase(diag)) diag = "";
            String exitInfo = "";
            try {
                if (proc != null && !proc.isAlive()) exitInfo = "exit=" + proc.exitValue();
            } catch (Throwable ignored) {}
            closeRootLocked();
            String detail = !diag.isEmpty() ? diag : (!exitInfo.isEmpty() ? exitInfo : "daemon_handshake_timeout");
            throw new IOException("特权工具进程握手未完成 (" + detail + ")");
        }
        if (!("READY:" + token).equals(handshake)) {
            String diag = lastDiag;
            if ("ok".equalsIgnoreCase(diag)) diag = "";
            closeRootLocked();
            throw new IOException("特权工具进程握手未完成" + (diag.isEmpty() ? " (invalid_token)" : " (" + diag + ")"));
        }
        daemonReady = true;
    }

    private void closeRootLocked() {
        boolean wasReady = daemonReady;
        daemonReady = false;
        try { if (rootIn != null) rootIn.close(); } catch (Exception ignored) {}
        try { if (rootOut != null) rootOut.close(); } catch (Exception ignored) {}
        rootIn = null;
        rootOut = null;
        if (adbStream != null) {
            try { adbStream.close(); } catch (Exception ignored) {}
            adbStream = null;
        }
        if (rootProcess != null) {
            try { rootProcess.destroyForcibly(); } catch (Exception ignored) {}
            rootProcess = null;
        }
        if (wasReady) notifyDaemonLost();
    }

    void forceResetDaemon() {
        boolean wasReady = daemonReady;
        daemonReady = false;
        if (wasReady) notifyDaemonLost();
        final java.lang.Process staleProc = rootProcess;
        final io.github.muntashirakon.adb.AdbStream staleAdb = adbStream;
        final BufferedReader staleIn = rootIn;
        final BufferedWriter staleOut = rootOut;
        rootProcess = null;
        adbStream = null;
        rootIn = null;
        rootOut = null;
        for (java.util.Map.Entry<String, CompletableFuture<String>> entry : pendingRpcs.entrySet()) {
            if (pendingRpcs.remove(entry.getKey(), entry.getValue())) {
                entry.getValue().completeExceptionally(new IOException("daemon_force_reset"));
            }
        }
        ioPool.submit(() -> {
            try { if (staleOut != null) { staleOut.write("{\"op\":\"release\",\"destroy\":true}\n"); staleOut.flush(); } } catch (Throwable ignored) {}
            try { if (staleIn != null) staleIn.close(); } catch (Throwable ignored) {}
            try { if (staleOut != null) staleOut.close(); } catch (Throwable ignored) {}
            try { if (staleAdb != null) staleAdb.close(); } catch (Throwable ignored) {}
            try { if (staleProc != null) staleProc.destroyForcibly(); } catch (Throwable ignored) {}
        });
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
                closeRootLocked();
            } finally {
                pipeLock.unlock();
            }
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            forceResetDaemon();
        }
    }
}
