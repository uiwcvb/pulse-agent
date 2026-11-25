package dev.bubuki.proactive;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.IBinder;
import android.os.ParcelFileDescriptor;
import kotlinx.coroutines.flow.MutableStateFlow;
import kotlinx.coroutines.flow.StateFlow;
import kotlinx.coroutines.flow.StateFlowKt;
import rikka.shizuku.Shizuku;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * Non-root Shizuku (v11+ / wireless debugging uid 2000) controller managing binder state,
 * runtime permission requests, UserService lifecycle, and dual-pipe RPC sessions.
 */
public final class ShizukuController {
    private static final String SHIZUKU_PACKAGE = "moe.shizuku.privileged.api";
    private static final int REQ_CODE_SHIZUKU = 99230;

    public static final class State {
        public final boolean installed;
        public final boolean binderAlive;
        public final boolean granted;
        public final boolean preV11;
        public final int serverUid;
        public final boolean serviceBound;
        public final String statusText;
        public final String lastError;

        State(
            boolean installed,
            boolean binderAlive,
            boolean granted,
            boolean preV11,
            int serverUid,
            boolean serviceBound,
            String statusText,
            String lastError
        ) {
            this.installed = installed;
            this.binderAlive = binderAlive;
            this.granted = granted;
            this.preV11 = preV11;
            this.serverUid = serverUid;
            this.serviceBound = serviceBound;
            this.statusText = statusText;
            this.lastError = lastError;
        }
    }

    static final class PipeSession {
        final InputStream in;
        final OutputStream out;
        final int uid;
        private volatile boolean closed = false;

        PipeSession(InputStream in, OutputStream out, int uid) {
            this.in = in;
            this.out = out;
            this.uid = uid;
        }

        boolean isClosed() {
            return closed;
        }

        void close() {
            closed = true;
            try { in.close(); } catch (Throwable ignored) {}
            try { out.close(); } catch (Throwable ignored) {}
        }
    }

    private static volatile ShizukuController instance;

    public static ShizukuController get(Context context) {
        if (instance == null) {
            synchronized (ShizukuController.class) {
                if (instance == null) {
                    instance = new ShizukuController(context.getApplicationContext());
                }
            }
        }
        return instance;
    }

    private final Context appContext;
    private final MutableStateFlow<State> stateFlow;
    private final Object serviceLock = new Object();
    private volatile IPulseShizukuService boundService;
    private volatile CompletableFuture<IPulseShizukuService> pendingBindFuture;
    private volatile Runnable onBinderDeadListener;
    private volatile Runnable onPermissionGrantedListener;
    private volatile String lastError = "";

    private final ServiceConnection serviceConnection = new ServiceConnection() {
        @Override
        public void onServiceConnected(ComponentName name, IBinder binder) {
            IPulseShizukuService svc = IPulseShizukuService.Stub.asInterface(binder);
            synchronized (serviceLock) {
                boundService = svc;
                CompletableFuture<IPulseShizukuService> fut = pendingBindFuture;
                if (fut != null && !fut.isDone() && svc != null) {
                    fut.complete(svc);
                }
            }
            refreshState();
        }

        @Override
        public void onServiceDisconnected(ComponentName name) {
            synchronized (serviceLock) {
                boundService = null;
                CompletableFuture<IPulseShizukuService> fut = pendingBindFuture;
                if (fut != null && !fut.isDone()) {
                    fut.completeExceptionally(new IllegalStateException("Shizuku UserService disconnected"));
                }
            }
            refreshState();
            Runnable dead = onBinderDeadListener;
            if (dead != null) {
                try { dead.run(); } catch (Throwable ignored) {}
            }
        }
    };

    private ShizukuController(Context context) {
        this.appContext = context.getApplicationContext();
        this.stateFlow = StateFlowKt.MutableStateFlow(computeSnapshot());
        try {
            Shizuku.addBinderReceivedListenerSticky(() -> {
                State st = computeSnapshot();
                stateFlow.setValue(st);
                if (st.binderAlive && !st.preV11 && st.granted) {
                    Runnable grantedCb = onPermissionGrantedListener;
                    if (grantedCb != null) {
                        try { grantedCb.run(); } catch (Throwable ignored) {}
                    }
                }
            });
            Shizuku.addBinderDeadListener(() -> {
                synchronized (serviceLock) {
                    boundService = null;
                }
                refreshState();
                Runnable dead = onBinderDeadListener;
                if (dead != null) {
                    try { dead.run(); } catch (Throwable ignored) {}
                }
            });
            Shizuku.addRequestPermissionResultListener((requestCode, grantResult) -> {
                if (requestCode != REQ_CODE_SHIZUKU) return;
                if (grantResult == PackageManager.PERMISSION_GRANTED) {
                    lastError = "";
                    refreshState();
                    Runnable grantedCb = onPermissionGrantedListener;
                    if (grantedCb != null) {
                        try { grantedCb.run(); } catch (Throwable ignored) {}
                    }
                } else {
                    lastError = "未授予 Shizuku 权限，请在 Shizuku 应用中允许 DSB";
                    refreshState();
                }
            });
        } catch (Throwable ignored) {}
    }

    void setOnBinderDeadListener(Runnable listener) {
        this.onBinderDeadListener = listener;
    }

    void setOnPermissionGrantedListener(Runnable listener) {
        this.onPermissionGrantedListener = listener;
    }

    public StateFlow<State> getStateFlow() {
        return stateFlow;
    }

    public State getState() {
        return stateFlow.getValue();
    }

    public void refreshState() {
        stateFlow.setValue(computeSnapshot());
    }

    private State computeSnapshot() {
        boolean installed = isShizukuInstalled();
        boolean binderAlive = false;
        try {
            binderAlive = Shizuku.pingBinder();
        } catch (Throwable ignored) {}
        boolean preV11 = false;
        boolean granted = false;
        int uid = -1;
        if (binderAlive) {
            try {
                preV11 = Shizuku.isPreV11();
                if (!preV11) {
                    granted = Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED;
                    uid = Shizuku.getUid();
                }
            } catch (Throwable ignored) {}
        }
        boolean svcAlive = isServiceAlive();
        String status;
        if (!installed) {
            status = "尚未安装 Shizuku（可用下方内置无线 ADB 替代）";
        } else if (!binderAlive) {
            status = "Shizuku 已安装但未启动，请打开 Shizuku 通过「无线调试」启动";
        } else if (preV11) {
            status = "Shizuku 版本过旧，请更新到 v11 以上版本";
        } else if (!granted) {
            status = "Shizuku 正在运行，等待授予权限";
        } else if (svcAlive) {
            status = "Shizuku 无线服务运行中 · 切 5G 流量也不掉线 (uid " + uid + ")";
        } else {
            status = "Shizuku 已授权 (uid " + uid + ") · 切 5G 流量也不掉线";
        }
        return new State(installed, binderAlive, granted, preV11, uid, svcAlive, status, lastError);
    }

    private boolean isShizukuInstalled() {
        try {
            appContext.getPackageManager().getPackageInfo(SHIZUKU_PACKAGE, 0);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    boolean isServiceAlive() {
        IPulseShizukuService svc = boundService;
        if (svc == null) return false;
        try {
            return svc.asBinder().isBinderAlive();
        } catch (Throwable ignored) {
            return false;
        }
    }

    boolean isReadyToBind() {
        State st = computeSnapshot();
        stateFlow.setValue(st);
        return st.binderAlive && !st.preV11 && st.granted;
    }

    boolean isRunningNeedingPermission() {
        State st = computeSnapshot();
        stateFlow.setValue(st);
        return st.binderAlive && !st.preV11 && !st.granted;
    }

    public void requestPermission() {
        State st = computeSnapshot();
        if (!st.installed) {
            lastError = "手机未安装 Shizuku，请先安装或使用下方内置无线 ADB";
            refreshState();
            return;
        }
        if (!st.binderAlive) {
            lastError = "Shizuku 服务未运行，请先打开 Shizuku 通过「无线调试」启动";
            refreshState();
            return;
        }
        if (st.preV11) {
            lastError = "Shizuku 版本过旧，请更新到 Shizuku v11+";
            refreshState();
            return;
        }
        if (!st.granted) {
            try {
                lastError = "";
                refreshState();
                Shizuku.requestPermission(REQ_CODE_SHIZUKU);
            } catch (Throwable t) {
                lastError = "拉起 Shizuku 授权失败: " + (t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName());
                refreshState();
            }
        } else {
            lastError = "";
            refreshState();
        }
    }

    public boolean openShizukuApp(Context ctx) {
        try {
            Intent intent = appContext.getPackageManager().getLaunchIntentForPackage(SHIZUKU_PACKAGE);
            if (intent != null) {
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                (ctx != null ? ctx : appContext).startActivity(intent);
                return true;
            }
        } catch (Throwable ignored) {}
        return false;
    }

    public void openShizukuDownloadPage(Context ctx) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse("https://shizuku.rikka.app/download/"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            (ctx != null ? ctx : appContext).startActivity(intent);
        } catch (Throwable ignored) {}
    }

    private Shizuku.UserServiceArgs buildUserServiceArgs() {
        return new Shizuku.UserServiceArgs(
            new ComponentName(appContext.getPackageName(), ShizukuDisplayUserService.class.getName())
        )
            .daemon(false)
            .processNameSuffix("shizuku")
            .tag("pulse_display_service")
            .debuggable(BuildConfig.DEBUG)
            .version(BuildConfig.VERSION_CODE);
    }

    private IPulseShizukuService ensureUserServiceBound(long timeoutMs) throws Exception {
        if (!isReadyToBind()) {
            throw new IllegalStateException("Shizuku 尚未运行或未授权");
        }
        IPulseShizukuService cur = boundService;
        if (cur != null && cur.asBinder().isBinderAlive()) {
            return cur;
        }
        CompletableFuture<IPulseShizukuService> bindFut;
        synchronized (serviceLock) {
            cur = boundService;
            if (cur != null && cur.asBinder().isBinderAlive()) {
                return cur;
            }
            if (pendingBindFuture != null && !pendingBindFuture.isDone()) {
                bindFut = pendingBindFuture;
            } else {
                bindFut = new CompletableFuture<>();
                pendingBindFuture = bindFut;
                try {
                    Shizuku.bindUserService(buildUserServiceArgs(), serviceConnection);
                } catch (Throwable t) {
                    bindFut.completeExceptionally(t);
                }
            }
        }
        try {
            IPulseShizukuService svc = bindFut.get(Math.max(2000L, timeoutMs), TimeUnit.MILLISECONDS);
            if (svc == null || !svc.asBinder().isBinderAlive()) {
                throw new IllegalStateException("Shizuku UserService 绑定失败");
            }
            return svc;
        } finally {
            synchronized (serviceLock) {
                if (pendingBindFuture == bindFut) {
                    pendingBindFuture = null;
                }
            }
        }
    }

    PipeSession openPipeSession(String token, long timeoutMs) throws Exception {
        IPulseShizukuService svc = ensureUserServiceBound(timeoutMs);
        ParcelFileDescriptor[] toServer = ParcelFileDescriptor.createPipe();
        ParcelFileDescriptor[] fromServer = ParcelFileDescriptor.createPipe();
        try {
            String apkPath = appContext.getApplicationInfo().sourceDir;
            svc.attachPipe(toServer[0], fromServer[1], token != null ? token : "", apkPath != null ? apkPath : "");
            int remoteUid = svc.getUid();
            InputStream in = new ParcelFileDescriptor.AutoCloseInputStream(fromServer[0]);
            OutputStream out = new ParcelFileDescriptor.AutoCloseOutputStream(toServer[1]);
            lastError = "";
            refreshState();
            return new PipeSession(in, out, remoteUid);
        } catch (Throwable t) {
            try { fromServer[0].close(); } catch (Throwable ignored) {}
            try { toServer[1].close(); } catch (Throwable ignored) {}
            throw t instanceof Exception ? (Exception) t : new Exception(t);
        } finally {
            // Remote process owns duplicates; closing local ends enables proper EOF when remote exits.
            try { toServer[0].close(); } catch (Throwable ignored) {}
            try { fromServer[1].close(); } catch (Throwable ignored) {}
        }
    }

    void unbindService(boolean destroyRemote) {
        synchronized (serviceLock) {
            boundService = null;
            CompletableFuture<IPulseShizukuService> fut = pendingBindFuture;
            pendingBindFuture = null;
            if (fut != null && !fut.isDone()) {
                fut.cancel(true);
            }
        }
        try {
            Shizuku.unbindUserService(buildUserServiceArgs(), serviceConnection, destroyRemote);
        } catch (Throwable ignored) {}
        refreshState();
    }
}
