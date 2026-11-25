package dev.bubuki.proactive;

import android.content.Context;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import androidx.annotation.Keep;
import org.json.JSONObject;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Shizuku UserService hosting {@link DisplayRuntime} under uid 2000 (or 0).
 * Uses dual {@link ParcelFileDescriptor} pipes rather than Binder transactions
 * so full-resolution screenshots and live frames never hit the 1MB Binder limit.
 */
public final class ShizukuDisplayUserService extends IPulseShizukuService.Stub {
    private static final String RPC_PREFIX = "PULSE_RPC:";
    private final Object runtimeLock = new Object();
    private final Object pipeSessionLock = new Object();
    private volatile DisplayRuntime runtime;
    private ParcelFileDescriptor activeReadFd;
    private ParcelFileDescriptor activeWriteFd;
    private Thread activeReaderThread;
    private volatile long pipeEpoch = 0L;
    private volatile boolean destroyed = false;
    private final ExecutorService rpcWorkers = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "pulse-shizuku-worker");
        t.setDaemon(true);
        return t;
    });

    @Keep
    public ShizukuDisplayUserService() {
    }

    @Keep
    public ShizukuDisplayUserService(Context ignoredContext) {
    }

    private DisplayRuntime getOrCreateRuntime() throws Exception {
        DisplayRuntime existing = runtime;
        if (existing != null) return existing;
        synchronized (runtimeLock) {
            if (runtime == null) {
                if (Looper.myLooper() == null) {
                    try {
                        Looper.prepare();
                    } catch (Throwable ignored) {}
                }
                runtime = new DisplayRuntime(DisplayRuntime.systemContext());
            }
            return runtime;
        }
    }

    @Override
    public int getUid() {
        return android.os.Process.myUid();
    }

    @Override
    public void attachPipe(ParcelFileDescriptor readFd, ParcelFileDescriptor writeFd, String token, String apkPath) {
        int uid = android.os.Process.myUid();
        if (uid != 0 && uid != 2000) {
            closeQuietly(readFd);
            closeQuietly(writeFd);
            throw new SecurityException("Shizuku display tools require uid 0 or 2000");
        }
        if (destroyed) {
            closeQuietly(readFd);
            closeQuietly(writeFd);
            throw new IllegalStateException("ShizukuDisplayUserService already destroyed");
        }
        if (apkPath != null && !apkPath.isEmpty()) {
            RootClipboard.setApkPath(apkPath);
        }
        final ParcelFileDescriptor prevRead;
        final ParcelFileDescriptor prevWrite;
        final long myEpoch;
        synchronized (pipeSessionLock) {
            prevRead = activeReadFd;
            prevWrite = activeWriteFd;
            activeReadFd = readFd;
            activeWriteFd = writeFd;
            myEpoch = ++pipeEpoch;
        }
        closeQuietly(prevRead);
        closeQuietly(prevWrite);

        final String expectedToken = token != null ? token : "";
        Thread t = new Thread(() -> runPipeLoop(myEpoch, readFd, writeFd, expectedToken), "pulse-shizuku-rpc");
        t.setDaemon(true);
        synchronized (pipeSessionLock) {
            activeReaderThread = t;
        }
        t.start();
    }

    private void runPipeLoop(long myEpoch, ParcelFileDescriptor readFd, ParcelFileDescriptor writeFd, String token) {
        PrintStream rpcOut = null;
        BufferedReader rpcIn = null;
        try {
            final DisplayRuntime rt = getOrCreateRuntime();
            rpcOut = new PrintStream(new ParcelFileDescriptor.AutoCloseOutputStream(writeFd), true, "UTF-8");
            rpcIn = new BufferedReader(new InputStreamReader(new ParcelFileDescriptor.AutoCloseInputStream(readFd), StandardCharsets.UTF_8));
            final PrintStream outRef = rpcOut;
            synchronized (outRef) {
                outRef.println(RPC_PREFIX + "READY:" + token);
                outRef.flush();
            }
            String line;
            while (!destroyed && pipeEpoch == myEpoch && (line = rpcIn.readLine()) != null) {
                final String reqLine = line.trim();
                if (reqLine.isEmpty()) continue;
                String reqId = "";
                try {
                    JSONObject parsed = new JSONObject(reqLine);
                    if ("exit".equals(parsed.optString("op"))) {
                        destroy();
                        return;
                    }
                    reqId = parsed.optString("_req_id", "");
                } catch (Throwable ignored) {}
                final String finalReqId = reqId;
                rpcWorkers.submit(() -> {
                    String response = rt.execute(reqLine);
                    if (!finalReqId.isEmpty()) {
                        try {
                            JSONObject respObj = new JSONObject(response);
                            respObj.put("_req_id", finalReqId);
                            response = respObj.toString();
                        } catch (Throwable ignored) {}
                    }
                    synchronized (outRef) {
                        outRef.println(RPC_PREFIX + response);
                        outRef.flush();
                    }
                });
            }
        } catch (Throwable t) {
            t.printStackTrace(System.err);
        } finally {
            synchronized (pipeSessionLock) {
                if (activeReadFd == readFd) activeReadFd = null;
                if (activeWriteFd == writeFd) activeWriteFd = null;
            }
            if (rpcIn != null) {
                try { rpcIn.close(); } catch (Throwable ignored) {}
            } else {
                closeQuietly(readFd);
            }
            if (rpcOut != null) {
                try { rpcOut.close(); } catch (Throwable ignored) {}
            } else {
                closeQuietly(writeFd);
            }
        }
    }

    @Override
    public void exit() {
        destroy();
    }

    @Override
    public void destroy() {
        destroyed = true;
        final ParcelFileDescriptor r;
        final ParcelFileDescriptor w;
        synchronized (pipeSessionLock) {
            pipeEpoch++;
            r = activeReadFd;
            w = activeWriteFd;
            activeReadFd = null;
            activeWriteFd = null;
        }
        closeQuietly(r);
        closeQuietly(w);
        rpcWorkers.shutdownNow();
        DisplayRuntime rt = runtime;
        runtime = null;
        if (rt != null) {
            try { rt.close(); } catch (Throwable ignored) {}
        }
        System.exit(0);
    }

    private static void closeQuietly(ParcelFileDescriptor pfd) {
        if (pfd != null) {
            try { pfd.close(); } catch (Throwable ignored) {}
        }
    }
}
