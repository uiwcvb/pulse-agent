package dev.bubuki.proactive;

import android.content.Context;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class SecurePrefs {
    private static final String ALIAS = "pulse.settings.v1";
    private static final String CACHE_PREFS = "pulse_cache";
    private static final String KEY_HOME_CACHE = "encrypted_home_cache";
    private static final String KEY_PHONE_HISTORY = "encrypted_phone_history";

    static final class FastConfig {
        final String serverUrl;
        final String deviceToken;
        final String publicKey;
        final String nodeSource;
        final boolean background;
        final String appearance;
        final boolean motion;
        final boolean haptics;
        final boolean powerSave;

        FastConfig(
            String serverUrl,
            String deviceToken,
            String publicKey,
            String nodeSource,
            boolean background,
            String appearance,
            boolean motion,
            boolean haptics,
            boolean powerSave
        ) {
            this.serverUrl = serverUrl;
            this.deviceToken = deviceToken;
            this.publicKey = publicKey;
            this.nodeSource = nodeSource;
            this.background = background;
            this.appearance = appearance;
            this.motion = motion;
            this.haptics = haptics;
            this.powerSave = powerSave;
        }
    }

    interface Mutator {
        void apply(JSONObject settings) throws Exception;
    }

    private static final Object HOME_CACHE_LOCK = new Object();
    private static final Object PHONE_HISTORY_LOCK = new Object();

    private static volatile SecretKey cachedSecretKey;
    private static volatile String cachedEncryptedRaw;
    private static volatile String cachedJsonStr;
    private static volatile FastConfig cachedFastConfig;

    private static volatile String cachedHomeRaw;
    private static volatile String cachedHomeJson;
    private static volatile String cachedPhoneRaw;
    private static volatile String cachedPhoneJson;

    private static SecretKey key() throws Exception {
        SecretKey existing = cachedSecretKey;
        if (existing != null) return existing;
        KeyStore store = KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        if (!store.containsAlias(ALIAS)) {
            KeyGenerator gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
            gen.init(new KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build());
            gen.generateKey();
        }
        SecretKey loaded = (SecretKey) store.getKey(ALIAS, null);
        cachedSecretKey = loaded;
        return loaded;
    }

    private static String encryptPacket(String jsonStr) throws Exception {
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE, key());
        JSONObject packet = new JSONObject()
            .put("iv", Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP))
            .put("data", Base64.encodeToString(cipher.doFinal(jsonStr.getBytes(StandardCharsets.UTF_8)), Base64.NO_WRAP));
        return packet.toString();
    }

    private static JSONObject decryptPacket(String raw) throws Exception {
        JSONObject data = new JSONObject(raw);
        Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE, key(), new GCMParameterSpec(128, Base64.decode(data.getString("iv"), Base64.NO_WRAP)));
        return new JSONObject(new String(cipher.doFinal(Base64.decode(data.getString("data"), Base64.NO_WRAP)), StandardCharsets.UTF_8));
    }

    private static void refreshFastConfigFrom(JSONObject result) {
        String srv = result.optString("serverUrl", SecureTransport.DEFAULT_SERVER).trim();
        if (srv.isEmpty()) srv = SecureTransport.DEFAULT_SERVER;
        String tok = result.optString("deviceToken", "").trim();
        if (tok.isEmpty()) tok = SecureTransport.defaultToken();
        String pub = result.optString("publicKey", "").trim();
        String src = result.optString("nodeSource", "").trim().toLowerCase(java.util.Locale.ROOT);
        boolean bg = result.optBoolean("background", true);
        String app = "light".equals(result.optString("appearance", "dark")) ? "light" : "dark";
        boolean mot = result.optBoolean("motion", true);
        boolean hap = result.optBoolean("haptics", true);
        boolean pwr = result.optBoolean("power_save", false);
        cachedFastConfig = new FastConfig(srv, tok, pub, src, bg, app, mot, hap, pwr);
    }

    static void syncUiFlagsInFastConfig(String appearance, boolean motion, boolean haptics, boolean powerSave) {
        FastConfig cur = cachedFastConfig;
        if (cur != null) {
            cachedFastConfig = new FastConfig(
                cur.serverUrl,
                cur.deviceToken,
                cur.publicKey,
                cur.nodeSource,
                cur.background,
                appearance,
                motion,
                haptics,
                powerSave
            );
        }
    }

    static FastConfig readFastConfig(Context context) {
        FastConfig snap = cachedFastConfig;
        if (snap != null) return snap;
        try {
            readFast(context);
            snap = cachedFastConfig;
            if (snap != null) return snap;
        } catch (Exception ignored) {}
        return new FastConfig(SecureTransport.DEFAULT_SERVER, SecureTransport.defaultToken(), "", "", true, "dark", true, true, false);
    }

    public static JSONObject readFast(Context context) throws Exception {
        String raw = context.getSharedPreferences("pulse", 0).getString("encrypted", "");
        String lastRaw = cachedEncryptedRaw;
        String lastJson = cachedJsonStr;
        if (lastRaw != null && lastJson != null && raw.equals(lastRaw)) {
            return new JSONObject(lastJson);
        }
        return read(context);
    }

    static synchronized JSONObject read(Context context) throws Exception {
        String raw = context.getSharedPreferences("pulse", 0).getString("encrypted", "");
        if (cachedEncryptedRaw != null && cachedJsonStr != null && raw.equals(cachedEncryptedRaw)) {
            JSONObject obj = new JSONObject(cachedJsonStr);
            if (cachedFastConfig == null) refreshFastConfigFrom(obj);
            return obj;
        }
        JSONObject result;
        if (raw.isEmpty()) {
            result = new JSONObject()
                .put("serverUrl", SecureTransport.DEFAULT_SERVER)
                .put("deviceToken", SecureTransport.defaultToken())
                .put("background", true);
        } else {
            result = decryptPacket(raw);
            if (result.optString("serverUrl", "").trim().isEmpty()) result.put("serverUrl", SecureTransport.DEFAULT_SERVER);
            if (result.optString("deviceToken", "").trim().isEmpty()) result.put("deviceToken", SecureTransport.defaultToken());
        }
        cachedEncryptedRaw = raw;
        cachedJsonStr = result.toString();
        refreshFastConfigFrom(result);
        return result;
    }

    static synchronized void write(Context context, JSONObject settings) throws Exception {
        save(context, settings);
    }

    static synchronized void save(Context context, JSONObject settings) throws Exception {
        saveInternal(context, settings, false);
    }

    static synchronized JSONObject update(Context context, Mutator mutator) throws Exception {
        JSONObject current = read(context);
        if (mutator != null) {
            mutator.apply(current);
        }
        current.remove("nativeHomeCache");
        current.remove("phoneHistory");
        current.remove("phoneTask");
        saveInternal(context, current, true);
        return new JSONObject(cachedJsonStr);
    }

    private static void saveInternal(Context context, JSONObject settings, boolean nonBlocking) throws Exception {
        settings.remove("nativeHomeCache");
        settings.remove("phoneHistory");
        settings.remove("phoneTask");
        String jsonStr = settings.toString();
        String rawPacket = encryptPacket(jsonStr);
        android.content.SharedPreferences.Editor ed = context.getSharedPreferences("pulse", 0).edit().putString("encrypted", rawPacket);
        if (nonBlocking) {
            ed.apply();
        } else {
            if (!ed.commit()) throw new Exception("本地加密保存失败");
        }
        cachedEncryptedRaw = rawPacket;
        cachedJsonStr = jsonStr;
        refreshFastConfigFrom(settings);
    }

    static JSONObject readHomeCache(Context context) {
        synchronized (HOME_CACHE_LOCK) {
            try {
                String raw = context.getApplicationContext().getSharedPreferences(CACHE_PREFS, Context.MODE_PRIVATE)
                    .getString(KEY_HOME_CACHE, "");
                if (raw != null && !raw.isEmpty()) {
                    if (cachedHomeRaw != null && cachedHomeJson != null && raw.equals(cachedHomeRaw)) {
                        return new JSONObject(cachedHomeJson);
                    }
                    JSONObject obj = decryptPacket(raw);
                    cachedHomeRaw = raw;
                    cachedHomeJson = obj.toString();
                    return obj;
                }
            } catch (Exception ignored) {}
        }
        try {
            JSONObject legacy = readFast(context).optJSONObject("nativeHomeCache");
            if (legacy != null) return legacy;
        } catch (Exception ignored) {}
        return null;
    }

    static void saveHomeCache(Context context, JSONObject cache) {
        if (cache == null) return;
        synchronized (HOME_CACHE_LOCK) {
            try {
                String jsonStr = cache.toString();
                String rawPacket = encryptPacket(jsonStr);
                context.getApplicationContext().getSharedPreferences(CACHE_PREFS, Context.MODE_PRIVATE)
                    .edit().putString(KEY_HOME_CACHE, rawPacket).apply();
                cachedHomeRaw = rawPacket;
                cachedHomeJson = jsonStr;
            } catch (Exception ignored) {}
        }
    }

    static JSONObject readPhoneHistory(Context context) {
        synchronized (PHONE_HISTORY_LOCK) {
            try {
                String raw = context.getApplicationContext().getSharedPreferences(CACHE_PREFS, Context.MODE_PRIVATE)
                    .getString(KEY_PHONE_HISTORY, "");
                if (raw != null && !raw.isEmpty()) {
                    if (cachedPhoneRaw != null && cachedPhoneJson != null && raw.equals(cachedPhoneRaw)) {
                        return new JSONObject(cachedPhoneJson);
                    }
                    JSONObject obj = decryptPacket(raw);
                    cachedPhoneRaw = raw;
                    cachedPhoneJson = obj.toString();
                    return obj;
                }
            } catch (Exception ignored) {}
        }
        try {
            JSONObject main = readFast(context);
            JSONObject legacy = new JSONObject();
            if (main.has("phoneHistory")) legacy.put("phoneHistory", main.optJSONArray("phoneHistory"));
            if (main.has("phoneTask")) legacy.put("phoneTask", main.optJSONObject("phoneTask"));
            return legacy;
        } catch (Exception ignored) {
            return new JSONObject();
        }
    }

    static void savePhoneHistory(Context context, JSONObject packet) {
        if (packet == null) return;
        synchronized (PHONE_HISTORY_LOCK) {
            try {
                String jsonStr = packet.toString();
                String rawPacket = encryptPacket(jsonStr);
                context.getApplicationContext().getSharedPreferences(CACHE_PREFS, Context.MODE_PRIVATE)
                    .edit().putString(KEY_PHONE_HISTORY, rawPacket).apply();
                cachedPhoneRaw = rawPacket;
                cachedPhoneJson = jsonStr;
            } catch (Exception ignored) {}
        }
    }
}
