package dev.bubuki.proactive;
import android.content.*;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.provider.Settings;
import android.view.*;
import android.widget.*;
import org.json.*;

final class OverlayController {
    private final Context context;
    private final WindowManager manager;
    private View view;
    private android.animation.ValueAnimator dimAnimator;
    private String current = "";
    private boolean replying = false;

    OverlayController(Context c) {
        context = c;
        manager = c.getSystemService(WindowManager.class);
    }

    private int dp(int v) {
        return (int) (v * context.getResources().getDisplayMetrics().density);
    }

    private TextView text(String t, int size, int color) {
        TextView v = new TextView(context);
        v.setText(t);
        v.setTextSize(size);
        v.setTextColor(color);
        v.setPadding(0, dp(6), 0, dp(6));
        return v;
    }

    private boolean animate() {
        try {
            return android.animation.ValueAnimator.areAnimatorsEnabled()
                && !PowerSaveController.isPowerSaveActive(context)
                && SecurePrefs.readFastConfig(context).motion;
        } catch (Exception ignored) {
            return false;
        }
    }

    void show(String id, String message) {
        show(id, "companion", "主助理", "重要提醒", message);
    }

    void show(String id, String botId, String botName, String titleText, String message) {
        if (!Settings.canDrawOverlays(context) || (id.equals(current) && view != null)) return;
        hide();
        current = id;
        String safeBotId = (botId == null || botId.trim().isEmpty()) ? "companion" : botId.trim();
        String safeBotName = (botName == null || botName.trim().isEmpty()) ? "智能助理" : botName.trim();
        String safeTitle = (titleText == null || titleText.trim().isEmpty()) ? "重要提醒" : titleText.trim();
        String safeBody = message == null ? "" : message.trim();
        String dupPrefix = "【" + safeTitle + "】";
        if (safeBody.startsWith(dupPrefix)) {
            safeBody = safeBody.substring(dupPrefix.length()).trim();
        }

        android.util.DisplayMetrics dm = context.getResources().getDisplayMetrics();
        int maxScrollH = Math.max(dp(120), (int) (dm.heightPixels * 0.36f));

        LinearLayout card = new LinearLayout(context);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(22), dp(18), dp(22), dp(18));
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(0xF5151720);
        bg.setCornerRadius(dp(26));
        bg.setStroke(Math.max(1, (int) (0.5f * dm.density)), 0xFF343846);
        card.setBackground(bg);
        card.setElevation(dp(24));

        LinearLayout headerRow = new LinearLayout(context);
        headerRow.setOrientation(LinearLayout.HORIZONTAL);
        headerRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView tag = text("●  DSB · " + safeBotName + " · 强通知", 12, 0xFF7DD3FC);
        tag.setTypeface(null, Typeface.BOLD);
        headerRow.addView(tag, new LinearLayout.LayoutParams(0, -2, 1f));
        TextView closeBtn = text("✕", 16, 0xFF969BB4);
        closeBtn.setPadding(dp(10), dp(4), dp(4), dp(4));
        closeBtn.setOnClickListener(v -> finish(id, 0, false, safeBotId));
        headerRow.addView(closeBtn);
        card.addView(headerRow);

        TextView title = text(safeTitle, 19, Color.WHITE);
        title.setTypeface(null, Typeface.BOLD);
        card.addView(title);

        ScrollView scroll = new ScrollView(context);
        scroll.setVerticalScrollBarEnabled(true);
        scroll.setScrollbarFadingEnabled(false);
        TextView bodyTv = text(safeBody, 15, 0xFFD2D5E6);
        bodyTv.setLineSpacing(dp(3), 1.0f);
        scroll.addView(bodyTv, new ScrollView.LayoutParams(-1, -2));
        int estLines = Math.max(1, safeBody.length() / 19 + safeBody.split("\n").length);
        int scrollH = estLines > 7 ? maxScrollH : ViewGroup.LayoutParams.WRAP_CONTENT;
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(-1, scrollH);
        sp.topMargin = dp(4);
        sp.bottomMargin = dp(8);
        card.addView(scroll, sp);

        LinearLayout btnRow = new LinearLayout(context);
        btnRow.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams rowLp = new LinearLayout.LayoutParams(-1, -2);
        rowLp.topMargin = dp(10);

        TextView openBtn = text("打开对话", 15, 0xFF10131A);
        openBtn.setTypeface(null, Typeface.BOLD);
        openBtn.setGravity(Gravity.CENTER);
        openBtn.setPadding(dp(12), dp(12), dp(12), dp(12));
        GradientDrawable openBg = new GradientDrawable();
        openBg.setColor(0xFFEEEEF3);
        openBg.setCornerRadius(dp(22));
        openBtn.setBackground(openBg);
        LinearLayout.LayoutParams openLp = new LinearLayout.LayoutParams(0, -2, 1.15f);
        openLp.rightMargin = dp(8);
        btnRow.addView(openBtn, openLp);

        TextView ackBtn = text("我知道了", 15, 0xFFE5E7F0);
        ackBtn.setGravity(Gravity.CENTER);
        ackBtn.setPadding(dp(12), dp(12), dp(12), dp(12));
        GradientDrawable ackBg = new GradientDrawable();
        ackBg.setColor(0xFF232735);
        ackBg.setCornerRadius(dp(22));
        ackBg.setStroke(Math.max(1, (int) (0.5f * dm.density)), 0xFF3A4054);
        ackBtn.setBackground(ackBg);
        LinearLayout.LayoutParams ackLp = new LinearLayout.LayoutParams(0, -2, 1f);
        btnRow.addView(ackBtn, ackLp);
        card.addView(btnRow, rowLp);

        TextView later = text("延后 15 分钟再提醒", 13, 0xFF9CA3AF);
        later.setGravity(Gravity.CENTER);
        later.setPadding(0, dp(12), 0, dp(2));
        card.addView(later);

        openBtn.setOnClickListener(v -> finish(id, 0, true, safeBotId));
        ackBtn.setOnClickListener(v -> finish(id, 0, false, safeBotId));
        later.setOnClickListener(v -> finish(id, 15, false, safeBotId));

        WindowManager.LayoutParams p = new WindowManager.LayoutParams(
            Math.min(dm.widthPixels - dp(32), dp(480)),
            -2,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_DIM_BEHIND | WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            android.graphics.PixelFormat.TRANSLUCENT
        );
        p.gravity = Gravity.CENTER;
        p.dimAmount = .62f;
        p.flags |= WindowManager.LayoutParams.FLAG_BLUR_BEHIND;
        p.setBlurBehindRadius(dp(20));
        view = card;
        try {
            boolean motion = animate();
            if (motion) {
                card.setAlpha(0);
                card.setTranslationY(dp(22));
                card.setScaleX(.98f);
                card.setScaleY(.98f);
                p.dimAmount = 0;
            }
            manager.addView(card, p);
            if (motion) {
                card.animate().alpha(1).translationY(0).scaleX(1).scaleY(1).setDuration(320)
                    .setInterpolator(new android.view.animation.PathInterpolator(.16f, 1f, .3f, 1f)).start();
                android.animation.ValueAnimator dim = android.animation.ValueAnimator.ofFloat(0, .62f);
                dim.setDuration(260);
                dim.addUpdateListener(a -> {
                    if (view == card) {
                        p.dimAmount = (float) a.getAnimatedValue();
                        try {
                            manager.updateViewLayout(card, p);
                        } catch (Exception ignored) {
                        }
                    }
                });
                dimAnimator = dim;
                dim.start();
            }
        } catch (Exception e) {
            view = null;
            current = "";
        }
    }

    private void finish(String id, int minutes, boolean openChat, String botId) {
        if (replying) return;
        replying = true;
        AgentForegroundService.clearPendingOverlay(id);
        hideAnimated();
        if (openChat) {
            try {
                Intent open = new Intent(context, NativePulseActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                    .putExtra("openBot", botId);
                context.startActivity(open);
            } catch (Exception ignored) {
            }
        }
        new Thread(() -> {
            try {
                JSONObject data = new JSONObject();
                if (minutes == 0) {
                    data.put("ids", new JSONArray().put(id));
                } else {
                    data.put("minutes", minutes);
                }
                new SecureTransport(context).request("POST", minutes == 0 ? "/api/ack" : "/api/snooze", data, null);
            } catch (Exception ignored) {
            }
        }, "pulse-overlay-reply").start();
    }

    private void hideAnimated() {
        if (dimAnimator != null) {
            dimAnimator.cancel();
            dimAnimator = null;
        }
        if (view == null || !animate()) {
            hide();
            return;
        }
        View card = view;
        card.animate().cancel();
        card.animate().alpha(0).translationY(dp(14)).scaleX(.985f).scaleY(.985f).setDuration(180)
            .setInterpolator(new android.view.animation.AccelerateInterpolator())
            .withEndAction(() -> {
                if (view == card) hide();
            }).start();
    }

    void hide() {
        if (dimAnimator != null) {
            dimAnimator.cancel();
            dimAnimator = null;
        }
        if (view != null) {
            view.animate().cancel();
            try {
                manager.removeView(view);
            } catch (Exception ignored) {
            }
            view = null;
        }
        current = "";
        replying = false;
    }
}
