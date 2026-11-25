package dev.bubuki.proactive;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.media.AudioAttributes;
import android.media.Ringtone;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.*;
import org.json.*;
import java.util.*;
import java.util.concurrent.*;
import okhttp3.*;

public class AgentForegroundService extends Service {
    static volatile boolean connected=false;
    static volatile boolean running=false;
    static volatile long phoneAbortUntilMs=0L;
    static volatile AgentForegroundService activeService=null;
    static void abortPhoneCommands(Context ctx){
        phoneAbortUntilMs=System.currentTimeMillis()+2500L;
        try{
            if(RootPhoneHarness.isVirtualDisplayActive()){
                RootPhoneHarness.stop(ctx, false);
            }
        }catch(Exception ignored){}
    }
    static void onUserForceClosedVirtualDisplay(Context ctx){
        phoneAbortUntilMs=System.currentTimeMillis()+10000L;
        try{
            if(RootPhoneHarness.isVirtualDisplayActive()){
                RootPhoneHarness.stop(ctx, false);
            }
        }catch(Exception ignored){}
        if(ctx!=null){
            final Context appCtx=ctx.getApplicationContext();
            new Thread(()->{
                try{
                    new SecureTransport(appCtx).request("POST","/api/stop",
                        new JSONObject().put("bot_id","all").put("source",SecureTransport.getNodeSource(appCtx)),null);
                }catch(Exception ignored){}
            },"vd-force-stop").start();
        }
    }
    public static final String ACTION_RECONNECT = "dev.bubuki.proactive.action.RECONNECT";
    private static volatile Ringtone activeAlarmRingtone=null;
    private static volatile Vibrator activeVibrator=null;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private final ExecutorService io=Executors.newSingleThreadExecutor();
    private final ExecutorService cmdPool=Executors.newCachedThreadPool();
    private volatile WebSocket socket;
    private volatile byte[] sessionKey;
    private volatile boolean stopped=false;
    private volatile int retrySeconds=2;
    private volatile long connectStartedMs=0L;
    private OverlayController overlay;
    private volatile JSONObject pendingOverlay;
    private BroadcastReceiver screen;
    private final LinkedHashSet<String> notified=new LinkedHashSet<>();
    private final LinkedHashMap<String,Integer> notifIdByMsgId=new LinkedHashMap<>();
    private final LinkedHashSet<String> nonces=new LinkedHashSet<>();
    private long snoozeUntil=0;
    private volatile long lastIdlePingSentMs=0L;
    private volatile long lastInboundWsMs=0L;
    private volatile long lastProbeSentMs=0L;
    private volatile boolean screenInteractive=true;
    private android.net.ConnectivityManager.NetworkCallback networkCallback=null;
    private final java.util.concurrent.atomic.AtomicLong wsGeneration=new java.util.concurrent.atomic.AtomicLong(0L);
    private final ConcurrentHashMap<String,CompletableFuture<JSONObject>> pendingRpcs=new ConcurrentHashMap<>();
    private final Runnable autoStopAlarmRunnable=()->stopEmergencyAlarm(this);

    public static final class ConnectionHealth {
        public final String phase;
        public final long wsGeneration;
        public final long lastInboundMs;
        public final long lastPongRttMs;
        public final int pendingRpcCount;
        public final String lastReason;
        public final boolean pcOnline;
        public final long updatedAtMs;

        public ConnectionHealth(String phase,long wsGeneration,long lastInboundMs,long lastPongRttMs,int pendingRpcCount,String lastReason,boolean pcOnline,long updatedAtMs){
            this.phase=phase;
            this.wsGeneration=wsGeneration;
            this.lastInboundMs=lastInboundMs;
            this.lastPongRttMs=lastPongRttMs;
            this.pendingRpcCount=pendingRpcCount;
            this.lastReason=lastReason;
            this.pcOnline=pcOnline;
            this.updatedAtMs=updatedAtMs;
        }
    }
    public interface HealthCallback { void onResult(ConnectionHealth h); }
    private static final java.util.concurrent.atomic.AtomicReference<ConnectionHealth> healthRef =
        new java.util.concurrent.atomic.AtomicReference<>(new ConnectionHealth("STOPPED",0L,0L,-1L,0,"未启动",false,0L));
    private volatile String lastDisconnectReason="正常";
    private volatile long lastPongRttMs=-1L;
    private volatile long lastManualProbeSentMs=0L;
    private volatile boolean lastPcOnline=false;
    private volatile CompletableFuture<Long> pendingProbeFuture=null;

    public static ConnectionHealth currentHealth(){
        AgentForegroundService svc=activeService;
        if(svc!=null){ svc.refreshHealth("sync"); }
        return healthRef.get();
    }

    private void refreshHealth(String phaseOverride){
        String ph=phaseOverride;
        if("sync".equals(ph)){
            ph=stopped?"STOPPED":(connected?"CONNECTED":(socket!=null?"CONNECTING":"RETRYING"));
        }
        healthRef.set(new ConnectionHealth(
            ph,
            wsGeneration.get(),
            lastInboundWsMs,
            lastPongRttMs,
            pendingRpcs.size(),
            lastDisconnectReason,
            lastPcOnline,
            System.currentTimeMillis()
        ));
    }

    private void safeExecute(ExecutorService pool, Runnable task) {
        if (stopped) return;
        try {
            pool.execute(task);
        } catch (RejectedExecutionException ignored) {}
    }

    private static boolean hasValidConfigForService(Context ctx) {
        if (ctx == null) return false;
        SecurePrefs.FastConfig cfg = SecurePrefs.readFastConfig(ctx);
        return cfg.background
            && cfg.serverUrl != null && !cfg.serverUrl.trim().isEmpty()
            && cfg.deviceToken != null && !cfg.deviceToken.trim().isEmpty();
    }

    public static void clearPendingOverlay(String id) {
        AgentForegroundService svc = activeService;
        if (svc == null) return;
        JSONObject cur = svc.pendingOverlay;
        if (cur != null && (id == null || id.isEmpty() || id.equals(cur.optString("id")))) {
            svc.pendingOverlay = null;
        }
    }

    public static void reconnectOrStart(Context ctx) {
        if (ctx == null) return;
        Context appCtx = ctx.getApplicationContext();
        AgentForegroundService svc = activeService;
        if (svc != null && !svc.stopped) {
            if (!hasValidConfigForService(appCtx)) {
                svc.handler.post(() -> svc.stopSelf());
                return;
            }
            svc.handler.post(() -> svc.forceReconnectSocket("config_changed"));
            return;
        }
        if (hasValidConfigForService(appCtx)) {
            try { appCtx.startForegroundService(new Intent(appCtx, AgentForegroundService.class)); } catch (Exception ignored) {}
        }
    }

    public static void forceReconnectFromUi(Context ctx){
        AgentForegroundService svc=activeService;
        if(svc==null||svc.stopped){
            if(ctx!=null && hasValidConfigForService(ctx)){
                try{
                    Intent si=new Intent(ctx.getApplicationContext(),AgentForegroundService.class);
                    ctx.getApplicationContext().startForegroundService(si);
                }catch(Exception ignored){}
            }
            return;
        }
        svc.handler.post(()->svc.forceReconnectSocket("manual_reconnect"));
    }

    public static void probeConnectionHealth(Context ctx,HealthCallback cb){
        AgentForegroundService svc=activeService;
        if(svc==null||svc.stopped||svc.socket==null||svc.sessionKey==null){
            ensureHealthyConnection(ctx);
            if(cb!=null){ new Handler(Looper.getMainLooper()).post(()->cb.onResult(currentHealth())); }
            return;
        }
        final WebSocket probedWs=svc.socket;
        final byte[] probedKey=svc.sessionKey;
        final long sentAt=System.currentTimeMillis();
        final CompletableFuture<Long> fut=new CompletableFuture<>();
        svc.pendingProbeFuture=fut;
        svc.lastManualProbeSentMs=sentAt;
        svc.safeExecute(svc.io, ()->{
            try{
                if(svc.socket==probedWs&&probedKey!=null){
                    int vdId=RootPhoneHarness.INSTANCE.currentDisplayId();
                    boolean ok=probedWs.send(SecureTransport.encrypt(probedKey,
                        new JSONObject().put("type","ping").put("virtual_display_id",vdId)).toString());
                    if(!ok) fut.completeExceptionally(new RuntimeException("ws_send_rejected"));
                }else{
                    fut.completeExceptionally(new RuntimeException("ws_stale"));
                }
            }catch(Exception e){
                fut.completeExceptionally(e);
            }
        });
        svc.safeExecute(svc.cmdPool, ()->{
            try{
                long rtt=fut.get(3500L,TimeUnit.MILLISECONDS);
                svc.lastPongRttMs=rtt;
                svc.lastDisconnectReason="探测成功 ("+rtt+"ms)";
                svc.refreshHealth("CONNECTED");
            }catch(Exception err){
                svc.lastPongRttMs=-1L;
                svc.lastDisconnectReason="探测超时，已触发重连";
                svc.handler.post(()->svc.forceReconnectIfSame(probedWs,"manual_probe_timeout"));
                svc.refreshHealth("RETRYING");
            }finally{
                if(svc.pendingProbeFuture==fut) svc.pendingProbeFuture=null;
                if(cb!=null){ svc.handler.post(()->cb.onResult(currentHealth())); }
            }
        });
    }

    static boolean isWsReady(){
        AgentForegroundService svc=activeService;
        return svc!=null&&connected&&!svc.stopped&&svc.socket!=null&&svc.sessionKey!=null;
    }

    public static void ensureHealthyConnection(Context ctx){
        AgentForegroundService svc=activeService;
        if(svc==null||svc.stopped){
            if(ctx!=null && hasValidConfigForService(ctx)){
                try{
                    Intent si=new Intent(ctx.getApplicationContext(),AgentForegroundService.class);
                    ctx.getApplicationContext().startForegroundService(si);
                }catch(Exception ignored){}
            }
            return;
        }
        svc.handler.post(()->svc.checkHealthOnHandler(false));
    }

    private void checkHealthOnHandler(boolean forceImmediateProbe) {
        if (stopped) return;
        if (!hasValidConfigForService(this)) {
            refreshHealth("UNPAIRED");
            return;
        }
        long now = System.currentTimeMillis();
        if (!isWsReady()) {
            // Respect an active in-flight handshake up to 10s so fast poll ticks never thrash it.
            if (socket != null && !connected && (now - connectStartedMs) < 10_000L) {
                return;
            }
            retrySeconds = 2;
            handler.removeCallbacks(reconnect);
            connect();
            return;
        }
        long lastIn = lastInboundWsMs;
        long idleMs = lastIn > 0 ? (now - lastIn) : 0L;
        if (lastIn > 0 && idleMs > 40_000L) {
            forceReconnectSocket("ws_idle_zombie");
            return;
        }
        if ((forceImmediateProbe && idleMs > 2_000L) || (idleMs > 12_000L && (now - lastProbeSentMs) > 4_000L)) {
            lastProbeSentMs = now;
            final WebSocket probedWs = socket;
            final byte[] probedKey = sessionKey;
            final long sentAt = now;
            safeExecute(io, () -> {
                try {
                    if (probedWs != null && probedKey != null) {
                        int vdId = RootPhoneHarness.INSTANCE.currentDisplayId();
                        probedWs.send(SecureTransport.encrypt(probedKey,
                            new JSONObject().put("type", "ping").put("virtual_display_id", vdId)).toString());
                    }
                } catch (Exception e) {
                    handler.post(() -> forceReconnectIfSame(probedWs, "ws_probe_send_failed"));
                }
            });
            handler.postDelayed(() -> {
                if (this.socket == probedWs && this.lastInboundWsMs < sentAt) {
                    forceReconnectIfSame(probedWs, "ws_probe_timeout");
                }
            }, 3500L);
        }
    }

    private void forceReconnectIfSame(WebSocket targetWs,String reason){
        if(targetWs!=null&&this.socket==targetWs){
            forceReconnectSocket(reason);
        }
    }

    private void forceReconnectSocket(String reason){
        connected=false;
        retrySeconds=2;
        lastDisconnectReason=reason!=null?reason:"reconnect";
        WebSocket old=this.socket;
        this.socket=null;
        failPendingRpcs(reason);
        if(old!=null){
            try{old.cancel();}catch(Throwable ignored){}
        }
        refreshHealth("RETRYING");
        handler.removeCallbacks(reconnect);
        connect();
    }

    private void failPendingRpcs(String reason){
        for(String k:new ArrayList<>(pendingRpcs.keySet())){
            CompletableFuture<JSONObject> f=pendingRpcs.remove(k);
            if(f!=null)f.completeExceptionally(new SecureTransport.WsRpcTransportException(true,reason));
        }
    }

    static JSONObject requestViaWs(String method,String path,JSONObject body,JSONObject query,long timeoutMs) throws Exception {
        AgentForegroundService svc=activeService;
        if(svc==null||!connected||svc.stopped)throw new SecureTransport.WsRpcTransportException(false,"ws_not_ready");
        WebSocket ws=svc.socket;
        byte[] key=svc.sessionKey;
        if(ws==null||key==null)throw new SecureTransport.WsRpcTransportException(false,"ws_no_session");
        String reqId=UUID.randomUUID().toString().replace("-","").substring(0,12);
        CompletableFuture<JSONObject> fut=new CompletableFuture<>();
        svc.pendingRpcs.put(reqId,fut);
        boolean sent=false;
        try{
            JSONObject pkt=new JSONObject().put("type","rpc_req").put("id",reqId).put("method",method).put("path",path);
            if(query!=null)pkt.put("query",query);
            if(body!=null)pkt.put("body",body);
            String rawFrame=SecureTransport.encrypt(key,pkt).toString();
            if(ws!=svc.socket||!connected||svc.stopped)throw new SecureTransport.WsRpcTransportException(false,"ws_stale");
            sent=ws.send(rawFrame);
            if(!sent)throw new SecureTransport.WsRpcTransportException(false,"ws_send_rejected");
            JSONObject res=fut.get(Math.max(2000L,timeoutMs),TimeUnit.MILLISECONDS);
            int status=res.optInt("status",200);
            JSONObject resBody=res.optJSONObject("body");
            if(resBody==null){
                Object rawBody=res.opt("body");
                resBody=new JSONObject();
                if(rawBody!=null&&rawBody!=JSONObject.NULL)resBody.put("data",rawBody);
            }
            if(status>=400){
                String msg=resBody.optString("detail",resBody.optString("error","请求失败 ("+status+")"));
                throw new SecureTransport.ApiHttpException(status,msg);
            }
            return resBody;
        }catch(SecureTransport.ApiHttpException|SecureTransport.WsRpcTransportException e){
            throw e;
        }catch(ExecutionException e){
            Throwable cause=e.getCause();
            if(cause instanceof SecureTransport.WsRpcTransportException)throw (SecureTransport.WsRpcTransportException)cause;
            throw new SecureTransport.WsRpcTransportException(sent,cause!=null?cause.getMessage():"ws_rpc_error");
        }catch(Exception e){
            throw new SecureTransport.WsRpcTransportException(sent,e.getMessage()!=null?e.getMessage():"ws_rpc_timeout");
        }finally{
            svc.pendingRpcs.remove(reqId,fut);
        }
    }

    static synchronized void stopEmergencyAlarm(Context ctx){
        try{
            if(activeAlarmRingtone!=null){
                if(activeAlarmRingtone.isPlaying()) activeAlarmRingtone.stop();
                activeAlarmRingtone=null;
            }
        }catch(Exception ignored){}
        try{
            if(activeVibrator!=null){
                activeVibrator.cancel();
                activeVibrator=null;
            }
        }catch(Exception ignored){}
        if(ctx!=null){
            try{
                ctx.getSystemService(NotificationManager.class).cancel(99119);
            }catch(Exception ignored){}
        }
    }

    @Override public void onCreate(){
        super.onCreate();activeService=this;running=true;overlay=new OverlayController(this);
        NotificationManager nm=getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel("pulse_connection","云端连接",NotificationManager.IMPORTANCE_LOW));
        nm.createNotificationChannel(new NotificationChannel("pulse_updates","助理状态更新",NotificationManager.IMPORTANCE_LOW));
        nm.createNotificationChannel(new NotificationChannel("pulse_nudges","好奇心与催单",NotificationManager.IMPORTANCE_HIGH));
        NotificationChannel alarmChan=new NotificationChannel("pulse_emergency_alarm","紧急闹钟提醒",NotificationManager.IMPORTANCE_HIGH);
        alarmChan.enableVibration(true);
        nm.createNotificationChannel(alarmChan);
        Intent open=new Intent(this,NativePulseActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_SINGLE_TOP);
        Notification notification=new Notification.Builder(this,"pulse_connection").setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle("共生 · 轻声等候").setContentText("加密连接日本节点 · 平时没有悬浮球")
            .setContentIntent(PendingIntent.getActivity(this,0,open,PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT))
            .setOngoing(true).setShowWhen(false).build();
        if(Build.VERSION.SDK_INT>=34)startForeground(10,notification,ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);else startForeground(10,notification);
        screen=new BroadcastReceiver(){@Override public void onReceive(Context c,Intent i){
            String act=i.getAction();
            if(Intent.ACTION_SCREEN_OFF.equals(act)){
                screenInteractive=false;
                overlay.hide();
                try{ RootPhoneHarness.onScreenInteractiveChanged(AgentForegroundService.this, false); }catch(Throwable ignored){}
            } else if(Intent.ACTION_USER_PRESENT.equals(act)||Intent.ACTION_SCREEN_ON.equals(act)){
                screenInteractive=true;
                try{ RootPhoneHarness.onScreenInteractiveChanged(AgentForegroundService.this, true); }catch(Throwable ignored){}
                tryShowOverlay();
                ensureHealthyConnection(AgentForegroundService.this);
            }
        }};
        IntentFilter filter=new IntentFilter();filter.addAction(Intent.ACTION_SCREEN_ON);filter.addAction(Intent.ACTION_SCREEN_OFF);filter.addAction(Intent.ACTION_USER_PRESENT);
        if(Build.VERSION.SDK_INT>=33)registerReceiver(screen,filter,Context.RECEIVER_NOT_EXPORTED);else registerReceiver(screen,filter);
        try{
            android.net.ConnectivityManager cm=getSystemService(android.net.ConnectivityManager.class);
            if(cm!=null){
                android.net.ConnectivityManager.NetworkCallback cb=new android.net.ConnectivityManager.NetworkCallback(){
                    @Override public void onAvailable(android.net.Network network){
                        handler.post(()->{
                            if(!stopped){
                                retrySeconds=2;
                                checkHealthOnHandler(true);
                            }
                        });
                    }
                    @Override public void onLost(android.net.Network network){
                        handler.post(()->{
                            if(!stopped){
                                lastDisconnectReason="network_lost";
                                refreshHealth("sync");
                            }
                        });
                    }
                };
                cm.registerDefaultNetworkCallback(cb);
                networkCallback=cb;
            }
        }catch(Throwable ignored){}
        connect();
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        if(intent!=null && ACTION_RECONNECT.equals(intent.getAction())){
            handler.post(()->forceReconnectSocket("config_changed"));
        }
        return START_STICKY;
    }
    private void connect(){
        if(stopped)return;
        if(!hasValidConfigForService(this)){
            refreshHealth("UNPAIRED");
            return;
        }
        connectStartedMs=System.currentTimeMillis();
        final long myGen=wsGeneration.incrementAndGet();
        safeExecute(io, ()->{try{
            if(stopped||wsGeneration.get()!=myGen)return;
            SecurePrefs.FastConfig cfg=SecurePrefs.readFastConfig(this);
            if(!cfg.background){handler.post(this::stopSelf);return;}
            if(!hasValidConfigForService(this)){refreshHealth("UNPAIRED");return;}
            WebSocket prev=socket;
            socket=null;
            if(prev!=null){try{prev.cancel();}catch(Throwable ignored){}}
            SecureTransport.Session s=new SecureTransport(this).session(new JSONObject(), true);
            sessionKey=s.key;
            synchronized(nonces){nonces.clear();}
            int pingSec=(PowerSaveController.isPowerSaveActive(this)||!screenInteractive)?45:30;
            OkHttpClient client=SecureTransport.HTTP.newBuilder()
                .pingInterval(pingSec,TimeUnit.SECONDS)
                .readTimeout(0,TimeUnit.SECONDS)
                .callTimeout(0,TimeUnit.SECONDS)
                .build();
            String url=s.base.replaceFirst("^http","ws")+"/ws/client";
            WebSocket createdWs=client.newWebSocket(new Request.Builder().url(url).build(),new WebSocketListener(){
                @Override public void onOpen(WebSocket ws,Response response){
                    if(wsGeneration.get()!=myGen){try{ws.cancel();}catch(Throwable ignored){} return;}
                    socket=ws;
                    lastInboundWsMs=System.currentTimeMillis();
                    ws.send(s.packet.toString());
                }
                @Override public void onMessage(WebSocket ws,String text){
                    if(wsGeneration.get()!=myGen||socket!=ws)return;
                    lastInboundWsMs=System.currentTimeMillis();
                    try{
                        JSONObject packet=new JSONObject(text);String nonce=packet.getString("nonce");
                        synchronized(nonces){
                            if(nonces.contains(nonce))return;
                            while(nonces.size()>=1000){
                                Iterator<String> it=nonces.iterator();
                                if(!it.hasNext())break;
                                it.next();it.remove();
                            }
                            nonces.add(nonce);
                        }
                        JSONObject event=SecureTransport.decrypt(s.key,packet);
                        if("rpc_res".equals(event.optString("type"))){
                            String reqId=event.optString("id","");
                            CompletableFuture<JSONObject> fut=pendingRpcs.remove(reqId);
                            if(fut!=null)fut.complete(event);
                            return;
                        }
                        handler.post(()->handle(event));
                    }catch(Exception e){
                        if(wsGeneration.get()==myGen&&socket==ws){
                            failPendingRpcs("ws_frame_error");
                            ws.close(1003,"invalid encrypted frame");
                        }
                    }
                }
                @Override public void onFailure(WebSocket ws,Throwable t,Response r){
                    if(wsGeneration.get()!=myGen||(socket!=null&&socket!=ws))return;
                    connected=false;socket=null;
                    lastDisconnectReason=(t!=null&&t.getMessage()!=null)?t.getMessage():"ws_failure";
                    failPendingRpcs("ws_failure");refreshHealth("RETRYING");handler.post(()->retry());
                }
                @Override public void onClosed(WebSocket ws,int code,String reason){
                    if(wsGeneration.get()!=myGen||(socket!=null&&socket!=ws))return;
                    connected=false;socket=null;
                    lastDisconnectReason=(reason!=null&&!reason.isEmpty())?reason:("ws_closed_"+code);
                    failPendingRpcs("ws_closed");refreshHealth("RETRYING");handler.post(()->retry());
                }
                @Override public void onClosing(WebSocket ws,int code,String reason){ws.close(code,reason);}
            });
            if(wsGeneration.get()==myGen){
                socket=createdWs;
            }else{
                try{createdWs.cancel();}catch(Throwable ignored){}
            }
        }catch(Exception e){if(wsGeneration.get()==myGen)handler.post(()->retry());}});
    }
    private void retry(){
        connected=false;broadcast();if(stopped)return;
        handler.removeCallbacks(reconnect);
        long baseMs=retrySeconds*1000L;
        long jitteredMs=Math.max(1000L,(long)(baseMs*(0.8+ThreadLocalRandom.current().nextDouble()*0.4)));
        handler.postDelayed(reconnect,jitteredMs);
        retrySeconds=Math.min(screenInteractive?60:120,retrySeconds*2);
    }
    private final Runnable reconnect=this::connect;
    private void handle(JSONObject event){
        String type=event.optString("type");
        String mySource=SecureTransport.getNodeSource(this);
        String evSource=event.optString("source","").trim();
        if(evSource.isEmpty()){
            JSONObject sub=event.optJSONObject("message");
            if(sub==null)sub=event.optJSONObject("stream");
            if(sub==null)sub=event.optJSONObject("execution");
            if(sub!=null)evSource=sub.optString("source","").trim();
        }
        if(!evSource.isEmpty()&&!evSource.equals(mySource)&&Arrays.asList("message","stream","execution","stopped","phone_workspace_command").contains(type)){
            return;
        }
        try{NativePulseModel.dispatchRealtimeEvent(event);}catch(Exception ignored){}
        if(type.equals("connected")){connected=true;retrySeconds=2;lastDisconnectReason="已握手连通";snoozeUntil=event.optLong("snooze_until")*1000;refreshHealth("CONNECTED");try{NativePulseModel.syncOnReconnect();}catch(Exception ignored){}}
        if(type.equals("pong")){
            lastPcOnline=event.optBoolean("dsh_online",false);
            long sentTs=Math.max(lastIdlePingSentMs,lastManualProbeSentMs);
            if(sentTs>0L){
                long rtt=Math.max(1L,System.currentTimeMillis()-sentTs);
                lastPongRttMs=rtt;
                CompletableFuture<Long> pf=pendingProbeFuture;
                if(pf!=null) pf.complete(rtt);
            }
            refreshHealth("CONNECTED");
            return;
        }
        if(type.equals("heartbeat")){
            int vdId=RootPhoneHarness.INSTANCE.currentDisplayId();
            long now=System.currentTimeMillis();
            long minPingGap=screenInteractive?25000L:50000L;
            if(vdId<=0 && now-lastIdlePingSentMs<minPingGap){
                refreshHealth("sync");
                return;
            }
            lastIdlePingSentMs=now;
            safeExecute(io, ()->{
                try{
                    final WebSocket ws=socket;
                    final byte[] key=sessionKey;
                    if(ws!=null&&key!=null){
                        ws.send(SecureTransport.encrypt(key,new JSONObject().put("type","ping").put("virtual_display_id",vdId)).toString());
                    }
                }catch(Exception ignored){}
            });
            return;
        }
        if(type.equals("snooze")){
            snoozeUntil=event.optLong("until")*1000;pendingOverlay=null;overlay.hide();
            stopEmergencyAlarm(this);
            NotificationManager nm=getSystemService(NotificationManager.class);
            for(String id:new ArrayList<>(notified)){
                Integer nid=notifIdByMsgId.remove(id);
                nm.cancel(nid!=null?nid:id.hashCode());
            }
        }
        if(type.equals("ack")){
            JSONArray ids=event.optJSONArray("ids");
            NotificationManager nm=getSystemService(NotificationManager.class);
            if(ids!=null)for(int i=0;i<ids.length();i++){
                String id=ids.optString(i);
                Integer nid=notifIdByMsgId.remove(id);
                nm.cancel(nid!=null?nid:id.hashCode());
                if(pendingOverlay!=null&&id.equals(pendingOverlay.optString("id"))){pendingOverlay=null;overlay.hide();}
            }
        }
        if(type.equals("emergency_alarm")){
            String action=event.optString("action","ring");
            if("stop".equals(action)){
                handler.removeCallbacks(autoStopAlarmRunnable);
                stopEmergencyAlarm(this);
            }else if(System.currentTimeMillis()>=snoozeUntil){
                String title=event.optString("title","🚨 紧急闹钟提醒");
                String body=event.optString("body","检测到十万火急事项，请立即查看！");
                String botId=event.optString("bot_id","companion");
                int durSec=Math.max(5,Math.min(30,event.optInt("duration_sec",15)));
                startEmergencyAlarm(title,body,botId,durSec);
            }
            return;
        }
        if(type.equals("phone_workspace_command")){
            String reqId=event.optString("request_id","");
            String op=event.optString("op","status");
            String botId=event.optString("bot_id","companion");
            JSONObject args=event.optJSONObject("args");
            if(args==null)args=new JSONObject();
            if("release".equals(op)&&args.optBoolean("force",false)){
                phoneAbortUntilMs=System.currentTimeMillis()+5000L;
            }
            final JSONObject cmdArgs=args;
            safeExecute(cmdPool, ()->{
                JSONObject res=new JSONObject();
                try{
                    if(!("release".equals(op)||"auto_release".equals(op))&&System.currentTimeMillis()<phoneAbortUntilMs){
                        throw new Exception("用户已停止执行");
                    }
                    if(("release".equals(op)||"auto_release".equals(op))&&!RootPhoneHarness.isVirtualDisplayActive()){
                        res=new JSONObject().put("ok",true).put("noop",true).put("created",false);
                    }else if(op.equals("run")||op.equals("task")){
                        String goal=cmdArgs.optString("goal",cmdArgs.optString("text","")).trim();
                        res=RootPhoneHarness.submitFromBridge(this,botId,goal);
                    }else{
                        JSONObject input=new JSONObject(cmdArgs.toString());
                        input.put("op",op);
                        int dispIdForSnap=-1;
                        String beforePkg="";
                        String beforeHash="";
                        int beforeNodes=-1;
                        long beforeFrameSeq=-1L;
                        String beforeImgSig="";
                        boolean skipTree = cmdArgs.optBoolean("skip_tree", false) || cmdArgs.optBoolean("pure_vision", false);
                        if(!skipTree && Arrays.asList("tap","tap_node","tap_and_type","long_press","batch","swipe","text","type_node","key","launch","open_uri","open_url").contains(op)){
                            dispIdForSnap=RootPhoneHarness.currentDisplayId();
                            if(dispIdForSnap>0){
                                PulseAccessibilityService.CachedSnapshot prevSnap=PulseAccessibilityService.getCachedSnapshot(dispIdForSnap);
                                if(prevSnap!=null){
                                    beforePkg=prevSnap.getActivePackage();
                                    beforeHash=prevSnap.getTreeHash();
                                    beforeNodes=prevSnap.getNodes().size();
                                }
                                try{
                                    JSONObject prevFrame=RootPhoneHarness.command(this,new JSONObject().put("op","preview").put("since",-1));
                                    beforeFrameSeq=prevFrame.optLong("frameSequence",-1L);
                                    String bImg=prevFrame.optString("image","");
                                    if(!bImg.isEmpty()) beforeImgSig=bImg.length()+":"+bImg.hashCode();
                                }catch(Exception ignored){}
                            }
                        }
                        res=RootPhoneHarness.command(this,input);
                        if(Arrays.asList("ui_tree","dump","tree","nodes","read_screen").contains(op) && !res.has("image")){
                            try{
                                JSONObject snap=RootPhoneHarness.command(this,new JSONObject().put("op","preview").put("since",-1).put("fresh",true));
                                if(snap.has("image"))res.put("image",snap.getString("image")).put("width",snap.optInt("width",540)).put("height",snap.optInt("height",1200));
                            }catch(Exception ignored){}
                        }
                        if(!skipTree && Arrays.asList("launch","open_uri","open_url","tap","tap_node","tap_and_type","long_press","batch","swipe","text","type_node","key").contains(op)){
                            int ncAfter=res.optInt("nodeCount",res.optInt("node_count",-1));
                            String afterPkg=res.optString("package","");
                            String afterHash=res.optString("treeHash",res.optString("tree_hash",""));
                            boolean needTreeFetch=!res.has("ui_tree") || ncAfter<0 || ((op.equals("launch")||op.equals("open_uri")||op.equals("open_url")) && ncAfter<=1);
                            if(needTreeFetch){
                                long settleMs=(op.equals("launch")||op.equals("open_uri")||op.equals("open_url")) ? 480L : 260L;
                                Thread.sleep(settleMs);
                                try{
                                    JSONObject tree=RootPhoneHarness.command(this,new JSONObject().put("op","ui_tree").put("refresh",true));
                                    ncAfter=tree.optInt("nodeCount",0);
                                    afterPkg=tree.optString("package","");
                                    afterHash=tree.optString("treeHash","");
                                    if(tree.has("summary")){
                                        res.put("ui_tree",tree.getString("summary"))
                                            .put("node_count",ncAfter)
                                            .put("nodeCount",ncAfter)
                                            .put("package",afterPkg)
                                            .put("tree_hash",afterHash)
                                            .put("treeHash",afterHash)
                                            .put("layoutStable",tree.optBoolean("layoutStable",true))
                                            .put("settled",tree.optBoolean("settled",true))
                                            .put("settle_ms",tree.optLong("settle_ms",settleMs))
                                            .put("densityDpi",tree.optInt("densityDpi",320))
                                            .put("screenWidthDp",tree.optInt("screenWidthDp",360))
                                            .put("fontScale",tree.optDouble("fontScale",1.0))
                                            .put("layout_signature",tree.optString("layout_signature",""));
                                    }
                                }catch(Exception ignored){}
                            }else{
                                res.put("node_count",ncAfter);
                            }
                            long afterFrameSeq=res.optLong("frameSequence",-1L);
                            String afterImgSig="";
                            String existingImg=res.optString("image","");
                            if(!existingImg.isEmpty()){
                                afterImgSig=existingImg.length()+":"+existingImg.hashCode();
                            }else{
                                try{
                                    JSONObject snap=RootPhoneHarness.command(this,new JSONObject().put("op","preview").put("since",-1).put("fresh",true));
                                    afterFrameSeq=snap.optLong("frameSequence",-1L);
                                    if(snap.has("image")){
                                        String aImg=snap.getString("image");
                                        if(!aImg.isEmpty()) afterImgSig=aImg.length()+":"+aImg.hashCode();
                                        res.put("image",aImg).put("width",snap.optInt("width",540)).put("height",snap.optInt("height",1200));
                                    }
                                }catch(Exception ignored){}
                            }
                            if(ncAfter<0) ncAfter=0;
                            boolean visualChanged=(afterFrameSeq>0 && afterFrameSeq!=beforeFrameSeq) || (!afterImgSig.isEmpty() && !afterImgSig.equals(beforeImgSig));
                            boolean treeChanged=beforeHash.isEmpty() || !afterHash.equals(beforeHash) || !afterPkg.equals(beforePkg) || beforeNodes!=ncAfter;
                            boolean changed=treeChanged || visualChanged;
                            String changeSummary=(changed ? "✅ 界面已变化: " : "⚠️ 界面未变: ")
                                + "包名 " + (beforePkg.isEmpty() ? "none" : beforePkg) + " -> " + (afterPkg.isEmpty() ? "unknown" : afterPkg)
                                + ", 节点数 " + (beforeNodes<0 ? "?" : beforeNodes) + " -> " + ncAfter
                                + (visualChanged && !treeChanged ? " (自绘画面帧已更新)" : "");
                            res.put("ui_changed",changed).put("visual_changed",visualChanged).put("change_summary",changeSummary);
                        }
                    }
                }catch(Exception e){
                    try{res.put("error",PulseErrors.safeError(e));}catch(Exception ignored){}
                }
                boolean sentViaWs=false;
                JSONObject reply=new JSONObject();
                try{
                    int vdId=RootPhoneHarness.INSTANCE.currentDisplayId();
                    if(!res.has("virtual_display_id") && vdId>0) res.put("virtual_display_id",vdId);
                    reply.put("type","phone_workspace_result").put("request_id",reqId).put("virtual_display_id",vdId).put("result",res);
                    final WebSocket ws=socket;
                    final byte[] key=sessionKey;
                    if(ws!=null&&key!=null){
                        sentViaWs=ws.send(SecureTransport.encrypt(key,reply).toString());
                    }
                }catch(Exception ignored){}
                boolean wsPotentiallyStale = lastInboundWsMs > 0 && (System.currentTimeMillis() - lastInboundWsMs > 30_000L);
                if(!sentViaWs || wsPotentiallyStale){
                    try{
                        new SecureTransport(this).request("POST","/api/phone/result",reply,null);
                    }catch(Exception ignored){}
                }
            });
            return;
        }
        if(type.equals("message")&&System.currentTimeMillis()>=snoozeUntil&&!NativePulseModel.isForeground()){
            JSONObject m=event.optJSONObject("message");
            if(m!=null&&"assistant".equals(m.optString("role"))&&!m.optBoolean("is_sub_turn",false)){
                String id=m.optString("id","");
                String text=m.optString("text","").trim();
                String kind=m.optString("kind","chat");
                boolean shouldNotify=m.optBoolean("notify","curiosity".equals(kind)||"deadline".equals(kind));
                if(shouldNotify&&!id.isEmpty()&&!text.isEmpty()&&!notified.contains(id)&&!kind.equals("stopped")){
                    String botId=m.optString("bot_id","companion");
                    String botName=m.optString("from_bot_name","").trim();
                    if(botName.isEmpty()) botName="companion".equals(botId)?"主助理":botId;
                    String title="curiosity".equals(kind)?(botName+" · 主动搭话"):botName;
                    boolean urgent="deadline".equals(kind)||"escalation_demo".equals(kind);
                    String collapseKey="msg:"+botId+":"+("curiosity".equals(kind)?"curiosity":"chat");
                    notifyCustom(id,title,text.length()>260?text.substring(0,260)+"…":text,botId,collapseKey,urgent);
                    rememberNotifiedId(id);
                }
            }
        }
        if(type.equals("user_notification")&&System.currentTimeMillis()>=snoozeUntil){
            String id=event.optString("id",UUID.randomUUID().toString());
            if(!notified.contains(id)){
                String title=event.optString("title","助理提醒");
                String body=event.optString("body","");
                String botId=event.optString("bot_id","companion");
                String botName=event.optString("bot_name","主助理");
                String collapseKey=event.optString("collapse_key","").trim();
                if(collapseKey.isEmpty()) collapseKey="notif:"+botId;
                boolean urgent=event.optBoolean("urgent",false);
                boolean overlayFlag=event.optBoolean("overlay",false)||"strong".equalsIgnoreCase(event.optString("level",""))||urgent;
                notifyCustom(id,title,body,botId,collapseKey,urgent||overlayFlag);
                rememberNotifiedId(id);
                if(overlayFlag){
                    try{
                        pendingOverlay=new JSONObject()
                            .put("id",id)
                            .put("bot_id",botId)
                            .put("bot_name",botName)
                            .put("title",title)
                            .put("text",body);
                        tryShowOverlay();
                    }catch(Exception ignored){}
                }
            }
        }
        if(type.equals("escalation")&&System.currentTimeMillis()>=snoozeUntil){
            int stage=event.optInt("stage");String id=event.optString("id");
            String text=event.optString("text","");JSONObject m=event.optJSONObject("message");if(m!=null)text=m.optString("text",text);
            if(stage==1&&!notified.contains(id)){
                String botId=m==null?event.optString("bot_id","companion"):m.optString("bot_id","companion");
                String botName=m!=null?m.optString("from_bot_name",event.optString("bot_name","默认助手")):event.optString("bot_name","默认助手");
                notifyCustom(id,botName,text,botId,"esc:"+botId,true);
                rememberNotifiedId(id);
            }
            if(stage==3){pendingOverlay=event;tryShowOverlay();}
        }
        if(!type.equals("stream")&&!type.equals("execution")&&!type.equals("cloud_desktop_frame")){
            broadcast();
        }
    }

    private void startEmergencyAlarm(String title,String body,String botId,int durSec){
        stopEmergencyAlarm(this);
        try{
            Uri alarmUri=RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
            if(alarmUri==null) alarmUri=RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            if(alarmUri==null) alarmUri=RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
            if(alarmUri!=null){
                Ringtone r=RingtoneManager.getRingtone(getApplicationContext(),alarmUri);
                if(r!=null){
                    r.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build());
                    r.setLooping(true);
                    r.play();
                    activeAlarmRingtone=r;
                }
            }
        }catch(Exception ignored){}
        try{
            Vibrator vib=getSystemService(Vibrator.class);
            if(vib!=null && vib.hasVibrator()){
                long[] pattern=new long[]{0,500,300,500,300,600};
                vib.vibrate(VibrationEffect.createWaveform(pattern,0));
                activeVibrator=vib;
            }
        }catch(Exception ignored){}
        handler.removeCallbacks(autoStopAlarmRunnable);
        handler.postDelayed(autoStopAlarmRunnable,durSec*1000L);

        Intent open=new Intent(this,NativePulseActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_SINGLE_TOP).putExtra("openBot",botId);
        PendingIntent tap=PendingIntent.getActivity(this,99119,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification n=new Notification.Builder(this,"pulse_emergency_alarm")
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(new Notification.BigTextStyle().bigText(body))
            .setContentIntent(tap)
            .setCategory(Notification.CATEGORY_ALARM)
            .setOngoing(true)
            .addAction(new Notification.Action.Builder(null,"关闭闹钟",AgentActionReceiver.pending(this,"stop_alarm","alarm")).build())
            .addAction(new Notification.Action.Builder(null,"免打扰 1 小时",AgentActionReceiver.pending(this,"snooze","alarm")).build())
            .build();
        try{getSystemService(NotificationManager.class).notify(99119,n);}catch(SecurityException ignored){}
    }

    private void tryShowOverlay(){
        if(pendingOverlay==null||System.currentTimeMillis()<snoozeUntil)return;
        PowerManager pm=getSystemService(PowerManager.class);KeyguardManager km=getSystemService(KeyguardManager.class);
        if(pm.isInteractive()&&!km.isKeyguardLocked()){
            JSONObject msgObj=pendingOverlay.optJSONObject("message");
            String id=pendingOverlay.optString("id","");
            String botId=pendingOverlay.optString("bot_id",msgObj!=null?msgObj.optString("bot_id","companion"):"companion");
            String botName=pendingOverlay.optString("bot_name",msgObj!=null?msgObj.optString("from_bot_name","主助理"):"主助理");
            String title=pendingOverlay.optString("title","重要提醒");
            String text=pendingOverlay.optString("text",msgObj!=null?msgObj.optString("text","这份成果等你拍板。"):"这份成果等你拍板。");
            overlay.show(id,botId,botName,title,text);
        }
    }
    private void rememberNotifiedId(String id) {
        if (id == null || id.isEmpty()) return;
        notified.remove(id);
        notified.add(id);
        while (notified.size() > 600) {
            Iterator<String> it = notified.iterator();
            if (!it.hasNext()) break;
            String oldest = it.next();
            it.remove();
            notifIdByMsgId.remove(oldest);
        }
    }

    private void notifyCustom(String id,String title,String text,String botId,String collapseKey,boolean urgent){
        String stableKey=(collapseKey!=null&&!collapseKey.isEmpty())?collapseKey:id;
        int notifId=20000+Math.abs(stableKey.hashCode()%10000);
        if (id != null && !id.isEmpty()) {
            notifIdByMsgId.put(id, notifId);
        }
        String channel=urgent?"pulse_nudges":"pulse_updates";
        Intent open=new Intent(this,NativePulseActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_SINGLE_TOP).putExtra("openBot",botId);
        PendingIntent tap=PendingIntent.getActivity(this,notifId,open,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification n=new Notification.Builder(this,channel).setSmallIcon(R.drawable.ic_launcher).setContentTitle(title)
            .setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text)).setContentIntent(tap).setAutoCancel(true)
            .setOnlyAlertOnce(!urgent)
            .setGroup("pulse_bot_"+botId)
            .setCategory(Notification.CATEGORY_REMINDER)
            .addAction(new Notification.Action.Builder(null,"我看完了",AgentActionReceiver.pending(this,"ack",id,notifId)).build())
            .addAction(new Notification.Action.Builder(null,"别吵 1 小时",AgentActionReceiver.pending(this,"snooze",id,notifId)).build()).build();
        try{getSystemService(NotificationManager.class).notify(notifId,n);}catch(SecurityException ignored){}
    }
    private void broadcast(){sendBroadcast(new Intent(getPackageName()+".UPDATE").setPackage(getPackageName()));}
    @Override public void onDestroy(){stopped=true;running=false;connected=false;if(activeService==this)activeService=null;failPendingRpcs("service_destroyed");stopEmergencyAlarm(this);handler.removeCallbacksAndMessages(null);if(networkCallback!=null){try{android.net.ConnectivityManager cm=getSystemService(android.net.ConnectivityManager.class);if(cm!=null)cm.unregisterNetworkCallback(networkCallback);}catch(Throwable ignored){}networkCallback=null;}WebSocket curWs=socket;socket=null;if(curWs!=null)curWs.close(1000,"stopped by user");overlay.hide();if(screen!=null)unregisterReceiver(screen);io.shutdownNow();cmdPool.shutdownNow();refreshHealth("STOPPED");super.onDestroy();}
    @Override public IBinder onBind(Intent i){return null;}
}
