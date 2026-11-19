package dev.bubuki.proactive;

import android.app.UiAutomation;
import android.graphics.Rect;
import android.os.HandlerThread;
import android.os.Looper;
import android.util.SparseArray;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import org.json.JSONArray;
import org.json.JSONObject;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.List;

/**
 * Privileged (uid 0 or uid 2000) UiAutomation UI-tree reader restricted strictly to VirtualDisplay (`displayId > 0`).
 * Never reads or falls back to physical Display 0.
 */
@android.annotation.SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "BlockedPrivateApi"})
final class VirtualDisplayUiTreeHelper {
    private static final int FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES = 0x00000001;
    private static UiAutomation cachedAutomation;
    private static HandlerThread automationThread;
    private static final Object LOCK = new Object();

    private VirtualDisplayUiTreeHelper() {}

    private static UiAutomation ensureConnected() throws Exception {
        synchronized (LOCK) {
            if (cachedAutomation != null) return cachedAutomation;
            if (automationThread == null) {
                automationThread = new HandlerThread("pulse-uiautomation-vd");
                automationThread.start();
            }
            Class<?> iConnCls = Class.forName("android.app.IUiAutomationConnection");
            Class<?> connImplCls = Class.forName("android.app.UiAutomationConnection");
            Object connImpl = connImplCls.getDeclaredConstructor().newInstance();
            Constructor<UiAutomation> ctor = UiAutomation.class.getDeclaredConstructor(Looper.class, iConnCls);
            ctor.setAccessible(true);
            UiAutomation uia = ctor.newInstance(automationThread.getLooper(), connImpl);
            try {
                Method connectWithFlags = UiAutomation.class.getMethod("connect", int.class);
                connectWithFlags.invoke(uia, FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES);
            } catch (Throwable ignored) {
                Method connectNoArg = UiAutomation.class.getMethod("connect");
                connectNoArg.invoke(uia);
            }
            cachedAutomation = uia;
            return uia;
        }
    }

    static JSONObject dumpDisplay(int displayId, int dispW, int dispH) {
        JSONObject out = new JSONObject();
        try {
            out.put("uid", android.os.Process.myUid());
            out.put("displayId", displayId);
            out.put("display_id", displayId);
            out.put("width", dispW);
            out.put("height", dispH);
            if (displayId <= 0) {
                return out.put("ok", false).put("error", "禁止读取 Display 0 UI 树");
            }
            UiAutomation uia = ensureConnected();
            List<AccessibilityWindowInfo> windows = null;
            try {
                SparseArray<List<AccessibilityWindowInfo>> all = uia.getWindowsOnAllDisplays();
                if (all != null) windows = all.get(displayId);
            } catch (Throwable ignored) {}
            if (windows == null) {
                // Only keep windows explicitly matching displayId (> 0)
                List<AccessibilityWindowInfo> raw = uia.getWindows();
                if (raw != null) {
                    windows = new ArrayList<>();
                    for (AccessibilityWindowInfo w : raw) {
                        if (w != null && w.getDisplayId() == displayId) windows.add(w);
                    }
                }
            }

            JSONArray nodesArr = new JSONArray();
            String activePkg = "";
            if (windows != null) {
                java.util.List<AccessibilityWindowInfo> sorted = new java.util.ArrayList<>();
                boolean hasExternal = false;
                for (AccessibilityWindowInfo w : windows) {
                    if (w == null) continue;
                    int type = w.getType();
                    if (type == AccessibilityWindowInfo.TYPE_INPUT_METHOD || type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) continue;
                    AccessibilityNodeInfo r = w.getRoot();
                    CharSequence pkg = r != null ? r.getPackageName() : null;
                    if (pkg != null && !"dev.bubuki.proactive".contentEquals(pkg)) hasExternal = true;
                    sorted.add(w);
                }
                sorted.sort((a, b) -> Integer.compare(b.getLayer(), a.getLayer()));
                int[] counter = new int[]{0};
                for (AccessibilityWindowInfo w : sorted) {
                    AccessibilityNodeInfo root = w.getRoot();
                    if (root == null) continue;
                    try {
                        CharSequence pkg = root.getPackageName();
                        if (hasExternal && pkg != null && "dev.bubuki.proactive".contentEquals(pkg)) continue;
                        if (activePkg.isEmpty() && pkg != null) {
                            activePkg = pkg.toString();
                        }
                        collectNodes(root, dispW, dispH, counter, nodesArr, 0);
                    } finally {
                        if (android.os.Build.VERSION.SDK_INT < 33) {
                            try { root.recycle(); } catch (Throwable ignored) {}
                        }
                    }
                }
            }
            out.put("ok", true)
                .put("package", activePkg)
                .put("nodeCount", nodesArr.length())
                .put("node_count", nodesArr.length())
                .put("nodes", nodesArr)
                .put("source", "ui_automation_vd");
            return out;
        } catch (Throwable err) {
            try {
                out.put("ok", false).put("error", err.getClass().getSimpleName() + ": " + err.getMessage());
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static void collectNodes(
        AccessibilityNodeInfo node,
        int dispW,
        int dispH,
        int[] counter,
        JSONArray out,
        int depth
    ) {
        if (node == null || depth > 28 || counter[0] >= 180) return;
        Rect r = new Rect();
        node.getBoundsInScreen(r);
        String text = node.getText() != null ? node.getText().toString().trim() : "";
        String desc = node.getContentDescription() != null ? node.getContentDescription().toString().trim() : "";
        String hint = node.getHintText() != null ? node.getHintText().toString().trim() : "";
        String resId = node.getViewIdResourceName() != null ? node.getViewIdResourceName() : "";
        boolean clickable = node.isClickable();
        boolean editable = node.isEditable();
        boolean scrollable = node.isScrollable();
        boolean visible = node.isVisibleToUser() && r.width() > 2 && r.height() > 2
            && r.right > 0 && r.bottom > 0 && r.left < dispW && r.top < dispH;

        if (visible && (clickable || editable || scrollable || !text.isEmpty() || !desc.isEmpty() || !hint.isEmpty())) {
            int idx = ++counter[0];
            String cls = node.getClassName() != null ? node.getClassName().toString() : "View";
            int dot = cls.lastIndexOf('.');
            String role = dot >= 0 ? cls.substring(dot + 1) : cls;
            String pkg = node.getPackageName() != null ? node.getPackageName().toString() : "";
            try {
                out.put(new JSONObject()
                    .put("ref", "@e" + idx)
                    .put("role", role)
                    .put("text", text)
                    .put("desc", desc)
                    .put("hint", hint)
                    .put("id", resId)
                    .put("pkg", pkg)
                    .put("left", Math.max(0, r.left))
                    .put("top", Math.max(0, r.top))
                    .put("right", Math.min(dispW, r.right))
                    .put("bottom", Math.min(dispH, r.bottom))
                    .put("center_x", Math.max(0, Math.min(dispW - 1, r.centerX())))
                    .put("center_y", Math.max(0, Math.min(dispH - 1, r.centerY())))
                    .put("clickable", clickable)
                    .put("editable", editable)
                    .put("scrollable", scrollable)
                    .put("checkable", node.isCheckable())
                    .put("checked", node.isChecked())
                );
            } catch (Throwable ignored) {}
        }
        int n = node.getChildCount();
        for (int i = 0; i < n; i++) {
            AccessibilityNodeInfo ch = node.getChild(i);
            if (ch != null) collectNodes(ch, dispW, dispH, counter, out, depth + 1);
        }
    }
}
