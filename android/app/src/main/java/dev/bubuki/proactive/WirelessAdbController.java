package dev.bubuki.proactive;

import android.annotation.SuppressLint;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import android.util.Log;
import androidx.annotation.RequiresApi;
import androidx.core.app.NotificationCompat;
import androidx.core.app.RemoteInput;
import io.github.muntashirakon.adb.AbsAdbConnectionManager;
import io.github.muntashirakon.adb.AdbStream;
import kotlinx.coroutines.flow.MutableStateFlow;
import kotlinx.coroutines.flow.StateFlow;
import kotlinx.coroutines.flow.StateFlowKt;
import org.conscrypt.Conscrypt;
import org.json.JSONObject;
import java.io.IOException;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.security.PrivateKey;
import java.security.Security;
import java.security.cert.Certificate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/** Android 11+ non-root Wireless Debugging controller (pairing via mDNS + RemoteInput notification, uid 2000 ADB shell). */
public final class WirelessAdbController {
    private static final String TAG = "WirelessAdbCtrl";
    private static final int NOTIF_PAIR_ID = 99220;
    private static final String CHANNEL_ID = "pulse_nudges";
    private static final String SERVICE_PAIRING = "_adb-tls-pairing._tcp.";
    private static final String SERVICE_CONNECT = "_adb-tls-connect._tcp.";

    public static final class State {
        public final boolean listening;
        public final boolean paired;
        public final boolean connected;
        public final String host;
        public final int pairingPort;
        public final int connectPort;
        public final String statusText;
        public final String lastError;

        State(boolean listening, boolean paired, boolean connected, String host, int pairingPort, int connectPort, String statusText, String lastError) {
            this.listening = listening;
            this.paired = paired;
            this.connected = connected;
            this.host = host;
            this.pairingPort = pairingPort;
            this.connectPort = connectPort;
            this.statusText = statusText;
            this.lastError = lastError;
        }
    }

    static {
        try {
            if (Security.getProvider("Conscrypt") == null) {
                Security.insertProviderAt(Conscrypt.newProvider(), 1);
            }
        } catch (Throwable ignored) {}
    }

    private static volatile WirelessAdbController instance;

    public static WirelessAdbController get(Context context) {
        if (instance == null) {
            synchronized (WirelessAdbController.class) {
                if (instance == null) {
                    instance = new WirelessAdbController(context.getApplicationContext());
                }
            }
        }
        return instance;
    }

    private final Context appContext;
    private final MutableStateFlow<State> stateFlow;
    private final ExecutorService executor = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "pulse-wireless-adb");
        t.setDaemon(true);
        return t;
    });
    private final Object adbLock = new Object();
    private final Object nsdLock = new Object();
    private final Object connectLock = new Object();
    private AbsAdbConnectionManager adbManager;
    private NsdManager nsdManager;
    private NsdManager.DiscoveryListener pairingListener;
    private NsdManager.DiscoveryListener connectListener;
    private final List<Object> serviceInfoCallbacks = new ArrayList<>();
    private volatile boolean listening;
    private volatile boolean paired;
    private volatile boolean connected;
    private volatile String discoveredHost = "127.0.0.1";
    private volatile int discoveredPairingPort = 0;
    private volatile int discoveredConnectPort = 0;
    private volatile String statusText = "未连接无线调试";
    private volatile String lastError = "";

    private WirelessAdbController(Context context) {
        this.appContext = context.getApplicationContext();
        try {
            JSONObject saved = SecurePrefs.readFast(this.appContext);
            this.paired = saved.optBoolean("adbPaired", false);
            String h = saved.optString("adbHost", "127.0.0.1").trim();
            if (!h.isEmpty()) this.discoveredHost = h;
            this.discoveredConnectPort = saved.optInt("adbConnectPort", 0);
            if (this.paired) {
                this.statusText = this.discoveredConnectPort > 0
                    ? "已配对（待连接端口 " + this.discoveredConnectPort + "）"
                    : "已配对（正在自动寻找连接端口）";
            }
        } catch (Throwable ignored) {}
        this.stateFlow = StateFlowKt.MutableStateFlow(snapshot());
    }

    public StateFlow<State> getStateFlow() {
        return stateFlow;
    }

    public State getState() {
        return stateFlow.getValue();
    }

    private State snapshot() {
        return new State(listening, paired, connected, discoveredHost, discoveredPairingPort, discoveredConnectPort, statusText, lastError);
    }

    private void emitState() {
        stateFlow.setValue(snapshot());
    }

    private void persistPrefs() {
        try {
            SecurePrefs.update(appContext, s -> {
                s.put("adbPaired", paired);
                s.put("adbHost", discoveredHost);
                s.put("adbConnectPort", discoveredConnectPort);
            });
        } catch (Throwable ignored) {}
    }

    static boolean isLocalDeviceAddress(InetAddress addr) {
        if (addr == null) return false;
        if (addr.isLoopbackAddress()) return true;
        try {
            Enumeration<NetworkInterface> ifaces = NetworkInterface.getNetworkInterfaces();
            if (ifaces == null) return false;
            byte[] targetBytes = addr.getAddress();
            while (ifaces.hasMoreElements()) {
                NetworkInterface iface = ifaces.nextElement();
                Enumeration<InetAddress> addrs = iface.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    InetAddress local = addrs.nextElement();
                    if (addr.equals(local) || Arrays.equals(targetBytes, local.getAddress())) {
                        return true;
                    }
                }
            }
        } catch (Throwable ignored) {}
        return false;
    }

    private AbsAdbConnectionManager getOrCreateManager() throws Exception {
        synchronized (adbLock) {
            if (adbManager != null) return adbManager;
            final AdbKeyPairStore.KeyMaterial km = AdbKeyPairStore.loadOrCreate(appContext);
            AbsAdbConnectionManager mgr = new AbsAdbConnectionManager() {
                {
                    setApi(Build.VERSION.SDK_INT);
                    setTimeout(8, TimeUnit.SECONDS);
                }
                @Override protected PrivateKey getPrivateKey() { return km.privateKey; }
                @Override protected Certificate getCertificate() { return km.certificate; }
                @Override protected String getDeviceName() { return "ProactiveAgent"; }
            };
            adbManager = mgr;
            return mgr;
        }
    }

    public void startDiscoveryAndShowNotification() {
        synchronized (nsdLock) {
            lastError = "";
            listening = true;
            statusText = "正在监听无线调试配对端口…";
            emitState();
            startNsdDiscoveryLocked();
        }
        postPairingNotification(null);
    }

    public void stopDiscoveryAndCancelNotification() {
        synchronized (nsdLock) {
            listening = false;
            stopNsdDiscoveryLocked();
            statusText = connected ? "已连接无线调试 (uid 2000)" : (paired ? "已配对，未连接" : "已停止配对监听");
            emitState();
        }
        NotificationManager nm = appContext.getSystemService(NotificationManager.class);
        if (nm != null) nm.cancel(NOTIF_PAIR_ID);
    }

    private void startNsdDiscoveryLocked() {
        stopNsdDiscoveryLocked();
        try {
            nsdManager = appContext.getSystemService(NsdManager.class);
            if (nsdManager == null) return;
            pairingListener = createDiscoveryListener(true);
            connectListener = createDiscoveryListener(false);
            nsdManager.discoverServices(SERVICE_PAIRING, NsdManager.PROTOCOL_DNS_SD, pairingListener);
            nsdManager.discoverServices(SERVICE_CONNECT, NsdManager.PROTOCOL_DNS_SD, connectListener);
        } catch (Throwable t) {
            lastError = "mDNS 监听启动失败: " + t.getMessage();
            emitState();
        }
    }

    private void stopNsdDiscoveryLocked() {
        if (nsdManager == null) return;
        if (Build.VERSION.SDK_INT >= 34 && !serviceInfoCallbacks.isEmpty()) {
            for (Object cb : serviceInfoCallbacks) {
                Api34Helper.unregisterCallback(nsdManager, cb);
            }
            serviceInfoCallbacks.clear();
        }
        try { if (pairingListener != null) nsdManager.stopServiceDiscovery(pairingListener); } catch (Throwable ignored) {}
        try { if (connectListener != null) nsdManager.stopServiceDiscovery(connectListener); } catch (Throwable ignored) {}
        pairingListener = null;
        connectListener = null;
    }

    private InetAddress pickLocalHostAddress(NsdServiceInfo info) {
        List<InetAddress> candidates;
        if (Build.VERSION.SDK_INT >= 34) {
            candidates = info.getHostAddresses();
        } else {
            @SuppressWarnings("deprecation")
            InetAddress legacyHost = info.getHost();
            candidates = legacyHost != null ? Collections.singletonList(legacyHost) : Collections.emptyList();
        }
        InetAddress fallbackLocal = null;
        for (InetAddress addr : candidates) {
            if (!isLocalDeviceAddress(addr)) continue;
            if (addr instanceof Inet4Address) return addr;
            if (fallbackLocal == null) fallbackLocal = addr;
        }
        return fallbackLocal;
    }

    private void handleResolvedServiceInfo(NsdServiceInfo info, boolean isPairing) {
        int port = info.getPort();
        if (port <= 0) return;
        InetAddress addr = pickLocalHostAddress(info);
        if (!isLocalDeviceAddress(addr)) {
            Log.w(TAG, "Ignoring non-local mDNS ADB service advertisement");
            return;
        }
        String hostStr = addr.getHostAddress();
        if (hostStr != null && !hostStr.isEmpty()) {
            discoveredHost = hostStr;
        }
        if (isPairing) {
            discoveredPairingPort = port;
            statusText = "已发现配对端口 " + port + "，下拉通知输入 6 位码";
            emitState();
        } else {
            discoveredConnectPort = port;
            persistPrefs();
            emitState();
            if (paired && !connected) {
                executor.execute(this::ensureConnected);
            }
        }
    }

    @RequiresApi(34)
    private static final class Api34Helper {
        static Object registerCallback(NsdManager mgr, NsdServiceInfo serviceInfo, ExecutorService exec, final WirelessAdbController owner, final boolean isPairing) {
            NsdManager.ServiceInfoCallback cb = new NsdManager.ServiceInfoCallback() {
                @Override public void onServiceInfoCallbackRegistrationFailed(int errorCode) {}
                @Override public void onServiceUpdated(NsdServiceInfo updatedInfo) {
                    owner.handleResolvedServiceInfo(updatedInfo, isPairing);
                }
                @Override public void onServiceLost() {}
                @Override public void onServiceInfoCallbackUnregistered() {}
            };
            mgr.registerServiceInfoCallback(serviceInfo, exec, cb);
            return cb;
        }

        static void unregisterCallback(NsdManager mgr, Object cb) {
            try {
                if (cb instanceof NsdManager.ServiceInfoCallback) {
                    mgr.unregisterServiceInfoCallback((NsdManager.ServiceInfoCallback) cb);
                }
            } catch (Throwable ignored) {}
        }
    }

    private NsdManager.DiscoveryListener createDiscoveryListener(final boolean isPairing) {
        return new NsdManager.DiscoveryListener() {
            @Override public void onStartDiscoveryFailed(String serviceType, int errorCode) {}
            @Override public void onStopDiscoveryFailed(String serviceType, int errorCode) {}
            @Override public void onDiscoveryStarted(String serviceType) {}
            @Override public void onDiscoveryStopped(String serviceType) {}
            @Override public void onServiceLost(NsdServiceInfo serviceInfo) {}
            @Override public void onServiceFound(NsdServiceInfo serviceInfo) {
                NsdManager mgr = nsdManager;
                if (mgr == null) return;
                try {
                    if (Build.VERSION.SDK_INT >= 34) {
                        synchronized (nsdLock) {
                            if (nsdManager != mgr) return;
                            Object cb = Api34Helper.registerCallback(mgr, serviceInfo, executor, WirelessAdbController.this, isPairing);
                            serviceInfoCallbacks.add(cb);
                        }
                    } else {
                        resolveLegacy(mgr, serviceInfo, isPairing);
                    }
                } catch (Throwable ignored) {}
            }
        };
    }

    @SuppressWarnings("deprecation")
    private void resolveLegacy(NsdManager mgr, NsdServiceInfo serviceInfo, final boolean isPairing) {
        mgr.resolveService(serviceInfo, new NsdManager.ResolveListener() {
            @Override public void onResolveFailed(NsdServiceInfo info, int errorCode) {}
            @Override public void onServiceResolved(NsdServiceInfo info) {
                handleResolvedServiceInfo(info, isPairing);
            }
        });
    }

    @SuppressLint("MissingPermission")
    private void postPairingNotification(String extraError) {
        try {
            NotificationManager nm = appContext.getSystemService(NotificationManager.class);
            if (nm == null) return;
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                nm.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "配对与提醒", NotificationManager.IMPORTANCE_HIGH));
            }
            RemoteInput remoteInput = new RemoteInput.Builder(WirelessAdbPairingReceiver.KEY_PAIRING_CODE)
                .setLabel("输入 6 位配对码")
                .build();
            Intent pairIntent = new Intent(appContext, WirelessAdbPairingReceiver.class)
                .setAction(WirelessAdbPairingReceiver.ACTION_SUBMIT_PAIRING_CODE);
            int mutFlag = PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE;
            PendingIntent pairPi = PendingIntent.getBroadcast(appContext, 99221, pairIntent, mutFlag);
            NotificationCompat.Action replyAction = new NotificationCompat.Action.Builder(
                R.drawable.ic_launcher, "输入配对码", pairPi
            ).addRemoteInput(remoteInput).setAllowGeneratedReplies(false).build();

            Intent stopIntent = new Intent(appContext, WirelessAdbPairingReceiver.class)
                .setAction(WirelessAdbPairingReceiver.ACTION_STOP_LISTENING);
            PendingIntent stopPi = PendingIntent.getBroadcast(appContext, 99222, stopIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

            String body = (extraError != null && !extraError.isEmpty())
                ? extraError
                : "请前往 系统设置 → 开发者选项 → 无线调试 → 使用配对码配对设备，然后下拉此通知输入 6 位数字";
            NotificationCompat.Builder b = new NotificationCompat.Builder(appContext, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle("免 Root 无线调试配对中")
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setOngoing(true)
                .addAction(replyAction)
                .addAction(0, "取消", stopPi);
            nm.notify(NOTIF_PAIR_ID, b.build());
        } catch (Throwable ignored) {}
    }

    @SuppressLint("MissingPermission")
    private void postConnectedNotification() {
        try {
            NotificationManager nm = appContext.getSystemService(NotificationManager.class);
            if (nm == null) return;
            NotificationCompat.Builder b = new NotificationCompat.Builder(appContext, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle("✅ 免 Root 无线调试已就绪")
                .setContentText("已通过本机 ADB 取得 uid=2000 隔离虚拟屏控制权限")
                .setAutoCancel(true)
                .setTimeoutAfter(6000L);
            nm.notify(NOTIF_PAIR_ID, b.build());
        } catch (Throwable ignored) {}
    }

    public void submitPairingCodeAsync(String rawCode, String manualHost, int manualPairPort, Runnable onDone) {
        final String digits = rawCode == null ? "" : rawCode.replaceAll("\\D+", "");
        if (digits.length() != 6) {
            lastError = "请输入正确的 6 位数字配对码";
            statusText = lastError;
            emitState();
            if (listening) postPairingNotification("⚠️ " + lastError);
            if (onDone != null) onDone.run();
            return;
        }
        executor.execute(() -> {
            try {
                AbsAdbConnectionManager mgr = getOrCreateManager();
                String targetHost = (manualHost != null && !manualHost.trim().isEmpty()) ? manualHost.trim() : discoveredHost;
                int targetPort = manualPairPort > 0 ? manualPairPort : discoveredPairingPort;
                if (targetPort <= 0) {
                    throw new IllegalStateException("未探测到配对端口，请先打开「使用配对码配对设备」弹窗，或在设置展开手动端口");
                }
                statusText = "正在与 " + targetHost + ":" + targetPort + " 配对…";
                lastError = "";
                emitState();
                boolean pairOk = false;
                try {
                    pairOk = mgr.pair(targetHost, targetPort, digits);
                } catch (Throwable firstErr) {
                    if (!"127.0.0.1".equals(targetHost)) {
                        pairOk = mgr.pair("127.0.0.1", targetPort, digits);
                        targetHost = "127.0.0.1";
                    } else {
                        throw firstErr;
                    }
                }
                if (!pairOk) throw new IOException("无线调试配对未通过，请确认配对码未过期");
                paired = true;
                discoveredHost = targetHost;
                persistPrefs();
                statusText = "配对成功，正在连接无线调试端口…";
                emitState();
                for (int i = 0; i < 12 && discoveredConnectPort <= 0; i++) {
                    Thread.sleep(250L);
                }
                boolean connOk = ensureConnected();
                if (!connOk && mgr.connectTls(appContext, 5000L)) {
                    connected = true;
                    connOk = true;
                }
                if (!connOk) {
                    throw new IOException("已配对但连接失败，请确认「无线调试」主开关保持开启");
                }
                listening = false;
                synchronized (nsdLock) {
                    stopNsdDiscoveryLocked();
                }
                statusText = "已连接免 Root 无线调试 (uid 2000)";
                lastError = "";
                emitState();
                postConnectedNotification();
                try { RootPhoneHarness.INSTANCE.status(appContext); } catch (Throwable ignored) {}
            } catch (Throwable t) {
                Log.w(TAG, "Wireless ADB pair failed", t);
                lastError = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
                statusText = "配对失败: " + lastError;
                emitState();
                if (listening) postPairingNotification("⚠️ " + lastError);
            } finally {
                if (onDone != null) onDone.run();
            }
        });
    }

    public boolean ensureConnected() {
        synchronized (connectLock) {
            try {
                AbsAdbConnectionManager mgr = getOrCreateManager();
                if (mgr.isConnected()) {
                    connected = true;
                    return true;
                }
                if (discoveredConnectPort > 0) {
                    String h = (discoveredHost != null && !discoveredHost.isEmpty()) ? discoveredHost : "127.0.0.1";
                    try {
                        if (mgr.connect(h, discoveredConnectPort)) {
                            connected = true;
                            statusText = "已连接免 Root 无线调试 (uid 2000)";
                            lastError = "";
                            emitState();
                            return true;
                        }
                    } catch (Throwable ignored) {}
                    if (!"127.0.0.1".equals(h)) {
                        try {
                            if (mgr.connect("127.0.0.1", discoveredConnectPort)) {
                                discoveredHost = "127.0.0.1";
                                connected = true;
                                persistPrefs();
                                statusText = "已连接免 Root 无线调试 (uid 2000)";
                                lastError = "";
                                emitState();
                                return true;
                            }
                        } catch (Throwable ignored) {}
                    }
                }
                if (paired && mgr.connectTls(appContext, 4000L)) {
                    connected = true;
                    statusText = "已连接免 Root 无线调试 (uid 2000)";
                    lastError = "";
                    emitState();
                    return true;
                }
            } catch (Throwable t) {
                lastError = t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName();
            }
            connected = false;
            emitState();
            return false;
        }
    }

    public AdbStream openShellStream(String command) throws Exception {
        if (!ensureConnected()) {
            throw new IOException("无线调试未连接");
        }
        AbsAdbConnectionManager mgr = getOrCreateManager();
        try {
            return mgr.openStream("exec:" + command);
        } catch (Throwable ignored) {
            return mgr.openStream("shell:stty -icanon -echo >/dev/null 2>&1; " + command);
        }
    }
}
