package dev.bubuki.proactive;

import android.graphics.Bitmap;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.Base64;
import android.view.Surface;
import java.io.ByteArrayOutputStream;
import java.util.concurrent.atomic.AtomicBoolean;
import org.json.JSONObject;

/**
 * Thread-safe VirtualDisplay ImageReader & preview encoding pipeline.
 * Serializes all `acquireLatestImage()` calls under `imageLock` so concurrent
 * fastPreview RPCs and frame callbacks never exhaust `maxImages = 2` or tear state.
 */
final class DisplayFramePipeline {
    interface StateProvider {
        JSONObject buildState() throws Exception;
    }

    private static final long FRAME_INTERVAL_ACTIVE_MS = 45L;
    private static final long FRAME_INTERVAL_IDLE_MS = 180L;
    private static final long FRAME_INTERVAL_DEEP_IDLE_MS = 420L;

    private final HandlerThread framesThread = new HandlerThread("pulse-root-frames");
    private final Handler frameHandler;
    private final Object frameLock = new Object();
    private final Object imageLock = new Object();
    private final Paint bitmapPaint = new Paint(Paint.FILTER_BITMAP_FLAG);
    private final AtomicBoolean framePending = new AtomicBoolean(false);

    private volatile VirtualDisplay boundDisplay;
    private volatile boolean releasedToBackground = false;
    private volatile ImageReader reader;
    private volatile int previewShortSide = 460;
    private volatile long lastLiveTouchAt = 0L;
    private volatile long lastInteractionAtMs = 0L;
    private volatile long lastPreviewRequestedAtMs = 0L;
    private volatile boolean skippedStaleFrames = false;
    private volatile long frameSequence = 0L;
    private volatile long lastFrameProcessedAt = 0L;
    private volatile DisplayFrameCodec.PreviewSnapshot latestPreview = null;

    private int bufWidth = 1472;
    private int bufHeight = 3232;
    private int frameRotation = 0;
    private Bitmap latestFrame = null;
    private Bitmap previewBuffer = null;

    private final Runnable processFrameRunnable = new Runnable() {
        @Override
        public void run() {
            framePending.set(false);
            lastFrameProcessedAt = SystemClock.uptimeMillis();
            processLatestFrame();
        }
    };

    DisplayFramePipeline() {
        framesThread.start();
        frameHandler = new Handler(framesThread.getLooper());
    }

    Handler frameHandler() {
        return frameHandler;
    }

    void setReleasedToBackground(boolean released) {
        this.releasedToBackground = released;
    }

    void markLiveTouch() {
        long now = SystemClock.uptimeMillis();
        lastLiveTouchAt = now;
        lastInteractionAtMs = now;
    }

    void markInteraction() {
        lastInteractionAtMs = SystemClock.uptimeMillis();
    }

    long lastInteractionAtMs() {
        return lastInteractionAtMs;
    }

    void setPreviewShortSide(int shortSide) {
        this.previewShortSide = shortSide;
        this.latestPreview = null;
    }

    Surface prepareSurface(int w, int h, int shortSide) {
        this.previewShortSide = shortSide;
        this.releasedToBackground = false;
        ImageReader newReader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2);
        synchronized (frameLock) {
            this.bufWidth = w;
            this.bufHeight = h;
            this.reader = newReader;
        }
        return newReader.getSurface();
    }

    void bindDisplay(VirtualDisplay vd) {
        this.boundDisplay = vd;
        ImageReader r = this.reader;
        if (r != null) {
            r.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
            frameHandler.post(this::scheduleFrameProcess);
        }
    }

    void reattachIfPresent(VirtualDisplay vd, int shortSide) {
        this.boundDisplay = vd;
        this.releasedToBackground = false;
        this.previewShortSide = shortSide;
        ImageReader r = this.reader;
        if (r != null) {
            r.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
            frameHandler.post(this::scheduleFrameProcess);
        }
    }

    /** Replaces Surface/ImageReader without holding `frameLock` during Binder calls. */
    void replaceSurfaceForResize(VirtualDisplay vd, int newW, int newH) {
        if (vd == null) return;
        ImageReader nextReader = ImageReader.newInstance(newW, newH, PixelFormat.RGBA_8888, 2);
        nextReader.setOnImageAvailableListener(this::onFrameAvailable, frameHandler);
        vd.setSurface(nextReader.getSurface());
        ImageReader oldReader;
        synchronized (frameLock) {
            oldReader = this.reader;
            this.reader = nextReader;
            this.bufWidth = newW;
            this.bufHeight = newH;
            this.latestPreview = null;
            if (latestFrame != null) {
                latestFrame.recycle();
                latestFrame = null;
            }
        }
        if (oldReader != null) {
            try {
                oldReader.setOnImageAvailableListener(null, null);
                oldReader.close();
            } catch (Throwable ignored) {}
        }
    }

    void releaseBuffers() {
        frameHandler.removeCallbacksAndMessages(null);
        framePending.set(false);
        this.boundDisplay = null;
        this.releasedToBackground = false;
        this.latestPreview = null;
        ImageReader oldReader;
        synchronized (imageLock) {
            synchronized (frameLock) {
                oldReader = this.reader;
                this.reader = null;
                if (latestFrame != null) {
                    latestFrame.recycle();
                    latestFrame = null;
                }
                if (previewBuffer != null) {
                    previewBuffer.recycle();
                    previewBuffer = null;
                }
                frameSequence = 0L;
            }
        }
        if (oldReader != null) {
            try {
                oldReader.setOnImageAvailableListener(null, null);
                oldReader.close();
            } catch (Throwable ignored) {}
        }
    }

    void quit() {
        framesThread.quitSafely();
    }

    void scheduleFrameProcess() {
        if (boundDisplay == null || releasedToBackground) return;
        if (!framePending.compareAndSet(false, true)) return;
        long now = SystemClock.uptimeMillis();
        long recentMs = Math.max(lastInteractionAtMs, lastPreviewRequestedAtMs);
        long interval;
        if (now - lastLiveTouchAt < 1200L) {
            interval = FRAME_INTERVAL_ACTIVE_MS;
        } else if (now - recentMs < 5000L) {
            interval = FRAME_INTERVAL_IDLE_MS;
        } else {
            interval = FRAME_INTERVAL_DEEP_IDLE_MS;
        }
        long wait = lastFrameProcessedAt + interval - now;
        if (wait > 0) {
            frameHandler.postDelayed(processFrameRunnable, wait);
        } else {
            frameHandler.post(processFrameRunnable);
        }
    }

    private void onFrameAvailable(ImageReader source) {
        if (releasedToBackground || boundDisplay == null) {
            synchronized (imageLock) {
                try (Image img = source.acquireLatestImage()) {} catch (Throwable ignored) {}
            }
            return;
        }
        long now = SystemClock.uptimeMillis();
        if (latestFrame != null && (now - lastPreviewRequestedAtMs > 4000L) && (now - lastInteractionAtMs > 4000L)) {
            synchronized (imageLock) {
                try (Image img = source.acquireLatestImage()) {} catch (Throwable ignored) {}
            }
            skippedStaleFrames = true;
            return;
        }
        scheduleFrameProcess();
    }

    private void processLatestFrame() {
        synchronized (imageLock) {
            ImageReader src = this.reader;
            VirtualDisplay vd = this.boundDisplay;
            if (src == null || vd == null) return;
            try (Image image = src.acquireLatestImage()) {
                if (image == null || releasedToBackground) return;
                int rot = vd.getDisplay().getRotation();
                synchronized (frameLock) {
                    if (this.reader != src || this.boundDisplay == null) return;
                    int bufH = this.bufHeight;
                    Image.Plane plane = image.getPlanes()[0];
                    int padded = plane.getRowStride() / plane.getPixelStride();
                    if (latestFrame == null || latestFrame.getWidth() != padded || latestFrame.getHeight() != bufH) {
                        if (latestFrame != null) latestFrame.recycle();
                        latestFrame = Bitmap.createBitmap(padded, bufH, Bitmap.Config.ARGB_8888);
                    }
                    latestFrame.copyPixelsFromBuffer(plane.getBuffer().rewind());
                    frameRotation = rot;
                    frameSequence++;
                }
            } catch (Throwable ignored) {}
        }
    }

    private DisplayFrameCodec.PreviewSnapshot encodeLatestPreviewOnDemand() {
        synchronized (frameLock) {
            if (latestFrame == null || boundDisplay == null) return latestPreview;
            long seq = frameSequence;
            if (latestPreview != null && latestPreview.sequence == seq) return latestPreview;
            Bitmap[] outBuf = new Bitmap[]{previewBuffer};
            DisplayFrameCodec.PreviewSnapshot snap = DisplayFrameCodec.encodePreview(
                latestFrame, seq, bufWidth, bufHeight, frameRotation,
                previewShortSide, previewBuffer, bitmapPaint, outBuf
            );
            previewBuffer = outBuf[0];
            this.latestPreview = snap;
            return snap;
        }
    }

    JSONObject fastPreview(JSONObject cmd, StateProvider stateProvider) throws Exception {
        VirtualDisplay vd = this.boundDisplay;
        if (vd == null || vd.getDisplay() == null || vd.getDisplay().getDisplayId() <= 0) {
            return stateProvider.buildState().put("hasFrame", false);
        }
        lastPreviewRequestedAtMs = SystemClock.uptimeMillis();
        if (cmd.optBoolean("fresh", false) || skippedStaleFrames) {
            skippedStaleFrames = false;
            processLatestFrame();
        }
        long since = cmd.optLong("since", -1);
        long curSeq = this.frameSequence;
        DisplayFrameCodec.PreviewSnapshot snap = this.latestPreview;
        if (since >= 0 && curSeq > 0 && curSeq == since && snap != null && snap.sequence == curSeq) {
            return stateProvider.buildState().put("hasFrame", true).put("unchanged", true).put("frameSequence", curSeq);
        }
        if (snap == null || snap.sequence != curSeq) {
            snap = encodeLatestPreviewOnDemand();
        }
        if (snap == null) {
            return stateProvider.buildState().put("hasFrame", false).put("frameSequence", 0);
        }
        if (since >= 0 && snap.sequence == since) {
            return stateProvider.buildState().put("hasFrame", true).put("unchanged", true).put("frameSequence", snap.sequence);
        }
        return stateProvider.buildState()
            .put("hasFrame", true)
            .put("width", snap.width)
            .put("height", snap.height)
            .put("image_width", snap.imageWidth)
            .put("image_height", snap.imageHeight)
            .put("rotation", snap.rotation)
            .put("frameSequence", snap.sequence)
            .put("frameHash", snap.frameHash)
            .put("image", snap.base64);
    }

    JSONObject capture(JSONObject cmd, StateProvider stateProvider) throws Exception {
        long now = SystemClock.uptimeMillis();
        lastInteractionAtMs = now;
        lastPreviewRequestedAtMs = now;
        skippedStaleFrames = false;
        processLatestFrame();
        Bitmap upright = null;
        long sequence = 0L;
        for (int i = 0; i < 40 && upright == null; i++) {
            synchronized (frameLock) {
                if (latestFrame != null) {
                    upright = DisplayFrameCodec.renderUprightBitmap(latestFrame, bufWidth, bufHeight, frameRotation);
                    sequence = frameSequence;
                }
            }
            if (upright == null) Thread.sleep(50);
        }
        if (upright == null) throw new Exception("尚未收到工作区画面，先在虚拟屏打开一个应用");
        int logicalW = upright.getWidth();
        int logicalH = upright.getHeight();
        Bitmap encodeBmp = upright;
        try {
            int minDim = Math.min(logicalW, logicalH);
            if (minDim > 720) {
                float capScale = 720f / (float) minDim;
                int encW = Math.max(1, Math.round(logicalW * capScale));
                int encH = Math.max(1, Math.round(logicalH * capScale));
                encodeBmp = Bitmap.createScaledBitmap(upright, encW, encH, true);
            }
            if (cmd.optBoolean("grid", false)) {
                DisplayInputHelper.drawAiGridOverlay(encodeBmp);
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream(65536);
            encodeBmp.compress(Bitmap.CompressFormat.JPEG, 84, out);
            return stateProvider.buildState()
                .put("width", logicalW)
                .put("height", logicalH)
                .put("image_width", encodeBmp.getWidth())
                .put("image_height", encodeBmp.getHeight())
                .put("frameSequence", sequence)
                .put("image", Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP));
        } finally {
            if (encodeBmp != null && encodeBmp != upright) {
                encodeBmp.recycle();
            }
            upright.recycle();
        }
    }
}
