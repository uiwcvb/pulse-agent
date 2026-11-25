package dev.bubuki.proactive;

/** Shared safe exception-to-user-message formatting helpers. */
public final class PulseErrors {
    private PulseErrors() {}

    public static String safeError(Throwable error) {
        if (error == null) return "未知错误";
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) {
            return error.getClass().getSimpleName();
        }
        return message.substring(0, Math.min(message.length(), 300));
    }
}
