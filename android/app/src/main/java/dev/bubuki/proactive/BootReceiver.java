package dev.bubuki.proactive;
import android.content.*;
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c,Intent i){try{
        if(!Intent.ACTION_BOOT_COMPLETED.equals(i.getAction()))return;
        SecurePrefs.FastConfig cfg = SecurePrefs.readFastConfig(c);
        boolean paired = cfg.serverUrl != null && !cfg.serverUrl.trim().isEmpty()
            && cfg.deviceToken != null && !cfg.deviceToken.trim().isEmpty();
        if(cfg.background && paired) c.startForegroundService(new Intent(c,AgentForegroundService.class));
    }catch(Exception ignored){}}
}
