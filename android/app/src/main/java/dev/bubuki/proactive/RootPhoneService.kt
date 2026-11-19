package dev.bubuki.proactive

import android.app.*
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.combine
import org.json.JSONObject

/** Keeps a user-started phone task OR live desktop virtual-display floating window alive across backgrounding. */
class RootPhoneService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var awake: PowerManager.WakeLock? = null
    override fun onCreate() {
        super.onCreate()
        val notifications = getSystemService(NotificationManager::class.java)
        notifications.createNotificationChannel(NotificationChannel("root-workspace", "手机工作区与小窗", NotificationManager.IMPORTANCE_LOW))
        promoteToForeground()
        scope.launch {
            var seenAlive = false
            combine(RootPhoneHarness.changes, FloatingVirtualDisplay.overlayVisible) { st, ov -> st to ov }
                .collect { (state, ovVisible) ->
                    val shouldKeepAlive = state.active || state.displayCreated || ovVisible ||
                        state.phase == "running" || state.phase == "waiting_user"
                    if (shouldKeepAlive) seenAlive = true
                    if (seenAlive && !shouldKeepAlive) {
                        if (awake?.isHeld == true) awake?.release()
                        awake = null
                        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
                        stopSelf()
                        return@collect
                    }
                    runCatching { notifications.notify(18192, notification(state, ovVisible)) }
                    if (state.phase == "running" && awake?.isHeld != true) {
                        awake = getSystemService(PowerManager::class.java)
                            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "$packageName:phone-task")
                            .apply { acquire(45 * 60 * 1000L) }
                    } else if (state.phase != "running") {
                        if (awake?.isHeld == true) awake?.release()
                        awake = null
                    }
                }
        }
    }
    private fun promoteToForeground() {
        runCatching {
            val notif = notification(RootPhoneHarness.changes.value, FloatingVirtualDisplay.overlayVisible.value)
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(18192, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(18192, notif)
            }
        }
    }
    private fun notification(state: RootPhoneState, overlayVisible: Boolean): Notification {
        val view = Intent(this, NativePulseActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra("openBot", state.botId)
        val stop = Intent(this, RootPhoneService::class.java).setAction("stop")
        val title = when {
            state.phase == "waiting_user" -> "手机工作区需要你处理"
            state.active || state.phase == "running" -> "手机工作区正在执行"
            else -> "手机虚拟屏小窗运行中"
        }
        val body = when {
            state.active || state.phase == "running" || state.phase == "waiting_user" -> "点击查看执行记录，或随时停止"
            overlayVisible -> "桌面悬浮小窗保活中 · 点击返回对话或关闭"
            else -> "虚拟屏运行中 · 点击查看或释放"
        }
        return Notification.Builder(this, "root-workspace").setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle(title)
            .setContentText(body)
            .setContentIntent(PendingIntent.getActivity(this, 18192, view, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE))
            .addAction(Notification.Action.Builder(null, "停止", PendingIntent.getService(this, 18193, stop, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)).build())
            .setVisibility(Notification.VISIBILITY_PRIVATE).setOngoing(true).build()
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        promoteToForeground()
        if (intent?.action == "stop") {
            FloatingVirtualDisplay.hide()
            if (RootPhoneHarness.changes.value.active) {
                RootPhoneHarness.stop(this, closeDisplay = true)
            } else {
                scope.launch(Dispatchers.IO) {
                    runCatching {
                        RootPhoneHarness.command(
                            applicationContext,
                            JSONObject().put("op", "release").put("destroy", true).put("user_close", true)
                        )
                    }
                    withContext(Dispatchers.Main) {
                        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
                        stopSelf()
                    }
                }
            }
        }
        return START_NOT_STICKY
    }
    override fun onDestroy() {
        scope.cancel()
        if (awake?.isHeld == true) awake?.release()
        awake = null
        if (RootPhoneHarness.changes.value.active) RootPhoneHarness.stop(this)
        super.onDestroy()
    }
    override fun onBind(intent: Intent?): IBinder? = null
}
