package dev.bubuki.proactive;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import androidx.core.app.RemoteInput;

/** Handles inline 6-digit Wireless Debugging pairing code entered directly from the notification shade. */
public final class WirelessAdbPairingReceiver extends BroadcastReceiver {
    static final String ACTION_SUBMIT_PAIRING_CODE = "dev.bubuki.proactive.ACTION_ADB_PAIR_CODE";
    static final String ACTION_STOP_LISTENING = "dev.bubuki.proactive.ACTION_ADB_STOP_LISTEN";
    static final String KEY_PAIRING_CODE = "pairing_code";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null) return;
        String action = intent.getAction();
        WirelessAdbController ctrl = WirelessAdbController.get(context);
        if (ACTION_STOP_LISTENING.equals(action)) {
            ctrl.stopDiscoveryAndCancelNotification();
            return;
        }
        if (ACTION_SUBMIT_PAIRING_CODE.equals(action)) {
            Bundle input = RemoteInput.getResultsFromIntent(intent);
            String rawCode = input != null ? input.getCharSequence(KEY_PAIRING_CODE, "").toString() : "";
            final PendingResult pr = goAsync();
            ctrl.submitPairingCodeAsync(rawCode, null, 0, () -> {
                try { pr.finish(); } catch (Throwable ignored) {}
            });
        }
    }
}
