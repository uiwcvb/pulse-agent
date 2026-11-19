package dev.bubuki.proactive;

import android.content.Context;
import android.util.Base64;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.zip.GZIPInputStream;
import java.util.zip.GZIPOutputStream;
import java.security.KeyFactory;
import java.security.SecureRandom;
import java.security.spec.MGF1ParameterSpec;
import java.security.spec.X509EncodedKeySpec;
import java.util.concurrent.TimeUnit;
import javax.crypto.Cipher;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.OAEPParameterSpec;
import javax.crypto.spec.PSource;
import javax.crypto.spec.SecretKeySpec;
import okhttp3.*;

final class SecureTransport {
    static final String DEFAULT_SERVER="";
    private static final String TOKEN_A="";
    private static final String TOKEN_B="";
    static String defaultToken(){return "";}
    static final OkHttpClient HTTP=new OkHttpClient.Builder().connectTimeout(30,TimeUnit.SECONDS)
        .readTimeout(300,TimeUnit.SECONDS).writeTimeout(60,TimeUnit.SECONDS).connectionPool(new ConnectionPool(5,25,TimeUnit.SECONDS)).retryOnConnectionFailure(true).build();
    static final MediaType JSON=MediaType.get("application/json; charset=utf-8");
    private static final SecureRandom RNG=new SecureRandom();
    private static volatile String cachedBase=null;
    private static volatile String cachedToken=null;
    private static volatile String cachedPubKeyB64=null;
    private static volatile byte[] cachedAesKey=null;
    private static volatile String cachedWrappedKeyB64=null;
    private static volatile long cachedAtMs=0L;

    static synchronized void invalidateCache(){
        cachedAesKey=null;
        cachedWrappedKeyB64=null;
        cachedAtMs=0L;
    }

    static String getNodeSource(Context context){
        try{
            JSONObject settings=SecurePrefs.readFast(context);
            String saved=settings.optString("nodeSource","").trim().toLowerCase();
            if(!saved.isEmpty())return saved;
        }catch(Exception ignored){}
        try{
            if(context.getResources().getConfiguration().smallestScreenWidthDp>=600){
                return "phone2";
            }
        }catch(Exception ignored){}
        return "phone";
    }

    private final Context context;
    SecureTransport(Context context){this.context=context.getApplicationContext();}
    static class Session {byte[] key;JSONObject packet;String base;}

    Session session(JSONObject inner) throws Exception {
        return session(inner, false);
    }

    Session session(JSONObject inner, boolean forceNewKey) throws Exception {
        long now=System.currentTimeMillis();
        String base=cachedBase;
        String token=cachedToken;
        String publicKey=cachedPubKeyB64;
        if(forceNewKey || base==null || token==null || publicKey==null || (now - cachedAtMs) > 15_000L){
            JSONObject settings=SecurePrefs.readFast(context);
            base=settings.optString("serverUrl",DEFAULT_SERVER).replaceAll("/+$","");
            HttpUrl url=HttpUrl.parse(base);
            if(url==null||(!url.scheme().equals("http")&&!url.scheme().equals("https"))||!url.username().isEmpty()||!url.password().isEmpty())throw new Exception("Server URL 无效");
            token=settings.optString("deviceToken","").trim();
            if(token.isEmpty())token=defaultToken();
            publicKey=settings.optString("publicKey","").trim();
            if(publicKey.isEmpty()){
                try(var in=context.getAssets().open("transport.json")){
                    publicKey=new JSONObject(new String(Io.read(in,20000),StandardCharsets.UTF_8)).getString("public_key");
                }
            }
            if(!base.equals(cachedBase) || !publicKey.equals(cachedPubKeyB64)){
                forceNewKey=true;
            }
            cachedBase=base;
            cachedToken=token;
            cachedPubKeyB64=publicKey;
        }
        byte[] aesKey=cachedAesKey;
        String wrappedKey=cachedWrappedKeyB64;
        if(forceNewKey || aesKey==null || wrappedKey==null || (now - cachedAtMs) > 1200_000L){
            aesKey=new byte[32];
            RNG.nextBytes(aesKey);
            Cipher rsa=Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding");
            rsa.init(Cipher.ENCRYPT_MODE,KeyFactory.getInstance("RSA").generatePublic(new X509EncodedKeySpec(Base64.decode(publicKey,Base64.DEFAULT))),
                new OAEPParameterSpec("SHA-256","MGF1",MGF1ParameterSpec.SHA256,PSource.PSpecified.DEFAULT));
            wrappedKey=Base64.encodeToString(rsa.doFinal(aesKey),Base64.NO_WRAP);
            cachedAesKey=aesKey;
            cachedWrappedKeyB64=wrappedKey;
            cachedAtMs=now;
        }
        Session result=new Session();
        result.key=aesKey;
        result.base=base;
        String nodeSource=getNodeSource(context);
        if(!inner.has("source"))inner.put("source",nodeSource);
        inner.put("token",token).put("timestamp",now/1000.0);
        result.packet=encrypt(result.key,inner);
        result.packet.put("key",wrappedKey);
        return result;
    }

    private static final int COMPRESS_THRESHOLD=384;
    static byte[] gzipCompress(byte[] raw) throws Exception {
        ByteArrayOutputStream bos=new ByteArrayOutputStream(raw.length);
        try(GZIPOutputStream gz=new GZIPOutputStream(bos)){gz.write(raw);}
        return bos.toByteArray();
    }
    static byte[] gzipDecompress(byte[] data) throws Exception {
        try(GZIPInputStream gis=new GZIPInputStream(new ByteArrayInputStream(data));
            ByteArrayOutputStream bos=new ByteArrayOutputStream(Math.max(256,data.length*2))){
            byte[] buf=new byte[4096];int n;long total=0L;
            while((n=gis.read(buf))!=-1){
                total+=n;
                if(total>12_000_000L)throw new Exception("解压后响应过大");
                bos.write(buf,0,n);
            }
            return bos.toByteArray();
        }
    }

    static JSONObject encrypt(byte[] key,JSONObject data) throws Exception {
        byte[] raw=data.toString().getBytes(StandardCharsets.UTF_8);
        boolean compressed=false;
        if(raw.length>=COMPRESS_THRESHOLD){
            byte[] gz=gzipCompress(raw);
            if(gz.length<raw.length){raw=gz;compressed=true;}
        }
        byte[] iv=new byte[12];RNG.nextBytes(iv);
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE,new SecretKeySpec(key,"AES"),new GCMParameterSpec(128,iv));
        JSONObject env=new JSONObject().put("nonce",Base64.encodeToString(iv,Base64.NO_WRAP))
            .put("data",Base64.encodeToString(cipher.doFinal(raw),Base64.NO_WRAP));
        if(compressed)env.put("z",1);
        return env;
    }

    static JSONObject decrypt(byte[] key,JSONObject packet) throws Exception {
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE,new SecretKeySpec(key,"AES"),new GCMParameterSpec(128,Base64.decode(packet.getString("nonce"),Base64.NO_WRAP)));
        byte[] raw=cipher.doFinal(Base64.decode(packet.getString("data"),Base64.NO_WRAP));
        if(packet.optInt("z",0)==1)raw=gzipDecompress(raw);
        return new JSONObject(new String(raw,StandardCharsets.UTF_8));
    }

    static final class ApiHttpException extends Exception {
        final int status;
        ApiHttpException(int status, String message) { super(message); this.status = status; }
    }
    static final class WsRpcTransportException extends Exception {
        final boolean sent;
        WsRpcTransportException(boolean sent, String message) { super(message); this.sent = sent; }
    }

    private static boolean isWsRpcEligible(String method, String path) {
        if ("/api/chat".equals(path) || "/api/message".equals(path) || "/api/vision".equals(path)
            || "/api/models".equals(path) || "/api/bots/export".equals(path) || "/api/bots/import".equals(path)
            || path.startsWith("/api/files")) {
            return false;
        }
        return true;
    }

    JSONObject request(String method,String path,JSONObject body,JSONObject query) throws Exception {
        if(!path.startsWith("/api/")||path.equals("/api/secure"))throw new Exception("不支持的 API 路径");
        if("/api/message".equals(path)){
            AgentForegroundService.phoneAbortUntilMs=0L;
            FloatingVirtualDisplay.resetUserDismissed();
        }
        String nodeSource=getNodeSource(context);
        if(query==null)query=new JSONObject();
        if(!query.has("source"))query.put("source",nodeSource);
        if(body!=null&&!body.has("source"))body.put("source",nodeSource);
        if(AgentForegroundService.isWsReady()&&isWsRpcEligible(method,path)){
            try{
                long timeoutMs="/api/context/compact".equals(path)||path.endsWith("/run")?120_000L:25_000L;
                return AgentForegroundService.requestViaWs(method,path,body,query,timeoutMs);
            }catch(ApiHttpException e){
                throw new Exception("HTTP "+e.status+" · "+e.getMessage());
            }catch(WsRpcTransportException e){
                boolean idempotentPost="/api/memories/pins".equals(path)||"/api/ack".equals(path)||"/api/snooze".equals(path)
                    ||"/api/stop".equals(path)||"/api/config".equals(path)||"/api/context/compact".equals(path)
                    ||(path.startsWith("/api/watchdogs/")&&path.endsWith("/stop"));
                if(e.sent&&"POST".equalsIgnoreCase(method)&&!idempotentPost){
                    throw new Exception("WS 请求传输中断: "+e.getMessage());
                }
            }
        }
        JSONObject inner=new JSONObject().put("method",method).put("path",path);
        if(body!=null)inner.put("body",body);inner.put("query",query);
        Session s=session(inner, false);
        Request req=new Request.Builder().url(s.base+"/api/secure").post(RequestBody.create(s.packet.toString(),JSON)).build();
        try(Response response=HTTP.newCall(req).execute()){
            if(response.code()==400){
                invalidateCache();
                s=session(inner, true);
                Request retryReq=new Request.Builder().url(s.base+"/api/secure").post(RequestBody.create(s.packet.toString(),JSON)).build();
                try(Response retryResp=HTTP.newCall(retryReq).execute()){
                    if(retryResp.body()==null)throw new Exception("服务器没有响应内容");
                    if(!retryResp.isSuccessful())throw new Exception("连接失败 HTTP "+retryResp.code()+"，检查服务器地址与设备时间");
                    JSONObject result=decrypt(s.key,new JSONObject(retryResp.body().string()));
                    if(result.getInt("status")>=400)throw new Exception("HTTP "+result.getInt("status")+" · "+result.getJSONObject("body").optString("detail","请求未完成"));
                    return result.getJSONObject("body");
                }
            }
            if(response.body()==null)throw new Exception("服务器没有响应内容");
            if(!response.isSuccessful())throw new Exception("连接失败 HTTP "+response.code()+"，检查服务器地址与设备时间");
            JSONObject result=decrypt(s.key,new JSONObject(response.body().string()));
            if(result.getInt("status")>=400)throw new Exception("HTTP "+result.getInt("status")+" · "+result.getJSONObject("body").optString("detail","请求未完成"));
            return result.getJSONObject("body");
        }
    }
}
