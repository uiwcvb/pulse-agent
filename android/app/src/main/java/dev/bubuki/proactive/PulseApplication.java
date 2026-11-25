package dev.bubuki.proactive;

/** Our own application process; phone tools share the same product state. */
public final class PulseApplication extends android.app.Application {
    @Override public void onCreate(){
        super.onCreate();
        String proc = getProcessName();
        if (proc == null) proc = "";
        if (proc.endsWith(":virtual_home")) {
            return;
        }
        android.content.SharedPreferences uiPrefs = getSharedPreferences("pulse_shared_ui", MODE_PRIVATE);
        if (!uiPrefs.contains("migrated")) {
            String legacy = "{}";
            try { legacy = SecurePrefs.read(this).toString(); } catch (Exception ignored) {}
            UiPreferences.initialize(this, legacy);
        } else {
            UiPreferences.initialize(this, "{}");
        }
    }
}
