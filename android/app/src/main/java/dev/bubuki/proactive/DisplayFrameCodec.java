package dev.bubuki.proactive;

import android.graphics.*;
import android.hardware.display.VirtualDisplay;
import android.util.Base64;
import java.io.ByteArrayOutputStream;
import org.json.JSONObject;

/** Stateless frame rotation, scaling, JPEG encoding and 64-bit luminance hashing for VirtualDisplay. */
final class DisplayFrameCodec {
    static final class PreviewSnapshot {
        final long sequence;
        final int width;
        final int height;
        final int imageWidth;
        final int imageHeight;
        final int rotation;
        final String base64;
        final String frameHash;

        PreviewSnapshot(
            long sequence,
            int width,
            int height,
            int imageWidth,
            int imageHeight,
            int rotation,
            String base64,
            String frameHash
        ) {
            this.sequence = sequence;
            this.width = width;
            this.height = height;
            this.imageWidth = imageWidth;
            this.imageHeight = imageHeight;
            this.rotation = rotation;
            this.base64 = base64;
            this.frameHash = frameHash;
        }
    }

    private DisplayFrameCodec() {}

    static String computeLuminanceHash64(Bitmap bmp) {
        if (bmp == null || bmp.getWidth() < 8 || bmp.getHeight() < 8) return "";
        int w = bmp.getWidth(), h = bmp.getHeight();
        int[] luma = new int[64];
        int sum = 0, idx = 0;
        for (int gy = 0; gy < 8; gy++) {
            int py = Math.min(h - 1, (gy * h + h / 2) / 8);
            for (int gx = 0; gx < 8; gx++) {
                int px = Math.min(w - 1, (gx * w + w / 2) / 8);
                int c = bmp.getPixel(px, py);
                int y = (((c >> 16) & 0xFF) * 77 + ((c >> 8) & 0xFF) * 150 + (c & 0xFF) * 29) >> 8;
                luma[idx++] = y;
                sum += y;
            }
        }
        int avg = sum / 64;
        long bits = 0L;
        for (int i = 0; i < 64; i++) {
            if (luma[i] >= avg) bits |= (1L << i);
        }
        return String.format("%016x", bits);
    }

    static Bitmap renderUprightBitmap(Bitmap latestFrame, int bufW, int bufH, int rot) {
        int w = rot % 2 == 0 ? bufW : bufH;
        int h = rot % 2 == 0 ? bufH : bufW;
        Bitmap upright = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(upright);
        if (rot == 1) { canvas.translate(0, bufW); canvas.rotate(-90); }
        else if (rot == 2) { canvas.translate(bufW, bufH); canvas.rotate(180); }
        else if (rot == 3) { canvas.translate(bufH, 0); canvas.rotate(90); }
        Rect bounds = new Rect(0, 0, bufW, bufH);
        canvas.drawBitmap(latestFrame, bounds, bounds, new Paint(Paint.FILTER_BITMAP_FLAG));
        return upright;
    }

    static PreviewSnapshot encodePreview(
        Bitmap latestFrame,
        long seq,
        int bufW,
        int bufH,
        int rot,
        int previewShortSide,
        Bitmap reuseBuffer,
        Paint bitmapPaint,
        Bitmap[] outReuseBuffer
    ) {
        int logicalW = rot % 2 == 0 ? bufW : bufH;
        int logicalH = rot % 2 == 0 ? bufH : bufW;
        int targetShort = Math.max(360, Math.min(logicalW, Math.min(logicalH, previewShortSide)));
        float scale = Math.min(1f, (float) targetShort / Math.min(logicalW, logicalH));
        int pw = Math.max(1, Math.round(logicalW * scale));
        int ph = Math.max(1, Math.round(logicalH * scale));
        Bitmap buffer = reuseBuffer;
        if (buffer == null || buffer.getWidth() != pw || buffer.getHeight() != ph) {
            if (buffer != null) buffer.recycle();
            buffer = Bitmap.createBitmap(pw, ph, Bitmap.Config.ARGB_8888);
        }
        outReuseBuffer[0] = buffer;
        Canvas canvas = new Canvas(buffer);
        canvas.scale((float) pw / logicalW, (float) ph / logicalH);
        if (rot == 1) { canvas.translate(0, bufW); canvas.rotate(-90); }
        else if (rot == 2) { canvas.translate(bufW, bufH); canvas.rotate(180); }
        else if (rot == 3) { canvas.translate(bufH, 0); canvas.rotate(90); }
        Rect bounds = new Rect(0, 0, bufW, bufH);
        canvas.drawBitmap(latestFrame, bounds, bounds, bitmapPaint);
        String fHash = computeLuminanceHash64(buffer);
        ByteArrayOutputStream out = new ByteArrayOutputStream(20480);
        buffer.compress(Bitmap.CompressFormat.JPEG, 72, out);
        String b64 = Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
        return new PreviewSnapshot(seq, logicalW, logicalH, pw, ph, rot, b64, fHash);
    }

    static JSONObject buildStateJson(
        VirtualDisplay vd,
        int width,
        int height,
        int densityDpi,
        boolean releasedToBackground,
        String activeTier,
        int lastPromotedTaskId,
        String lastPromotedPkg,
        boolean trustedDisplay
    ) throws Exception {
        int r = (vd == null || vd.getDisplay() == null) ? 0 : vd.getDisplay().getRotation();
        int lw = r % 2 == 0 ? width : height;
        int lh = r % 2 == 0 ? height : width;
        int dispId = (vd == null || vd.getDisplay() == null) ? -1 : vd.getDisplay().getDisplayId();
        boolean activeCreated = vd != null && dispId > 0 && !releasedToBackground;
        int swDp = Math.round((Math.min(lw, lh) * 160f) / Math.max(120, densityDpi));
        return new JSONObject()
            .put("created", activeCreated)
            .put("display_active", activeCreated)
            .put("newly_created", false)
            .put("allocated", vd != null && dispId > 0)
            .put("released", releasedToBackground)
            .put("displayId", dispId)
            .put("display_id", dispId)
            .put("width", lw)
            .put("height", lh)
            .put("dpi", densityDpi)
            .put("densityDpi", densityDpi)
            .put("tier", activeTier)
            .put("swDp", swDp)
            .put("screenWidthDp", swDp)
            .put("fontScale", 1.0f)
            .put("rotation", r)
            .put("uid", android.os.Process.myUid())
            .put("hasPromotedTask", lastPromotedTaskId > 0)
            .put("promotedTaskId", lastPromotedTaskId)
            .put("promotedPackage", lastPromotedPkg)
            .put("isolatedFocus", vd != null && trustedDisplay);
    }
}
