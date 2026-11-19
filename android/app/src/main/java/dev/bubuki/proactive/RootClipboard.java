package dev.bubuki.proactive;

import android.content.Context;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import org.json.JSONObject;

/** Root or Wireless-ADB (uid 0 / uid 2000) authorized paste into isolated VirtualDisplay only. */
final class RootClipboard {
    RootClipboard(Context ignored){}
    private static boolean isPrivilegedUid() {
        int u = android.os.Process.myUid();
        return u == 0 || u == 2000;
    }
    private static String line(BufferedReader input)throws Exception {
        FutureTask<String> read=new FutureTask<>(input::readLine);Thread reader=new Thread(read,"pulse-clipboard-reply");reader.setDaemon(true);reader.start();
        String value=read.get(8,TimeUnit.SECONDS);if(value==null)throw new IOException("剪贴板会话已断开");
        JSONObject result=new JSONObject(value);if(result.has("error"))throw new IOException(result.getString("error"));return value;
    }
    void paste(int displayId, String text) throws Exception {
        paste(displayId, text, false);
    }
    void paste(int displayId, String text, boolean clear) throws Exception {
        if(!isPrivilegedUid()||displayId<=0)throw new SecurityException("只允许特权会话向独立虚拟屏输入");
        if (clear) {
            DisplayInputHelper.injectClearField(displayId);
        }
        if (text == null || text.isEmpty()) return;
        ProcessBuilder pb = new ProcessBuilder("/system/bin/app_process","/system/bin","dev.bubuki.proactive.RootClipboardBroker").redirectError(new File("/dev/null"));
        String cp = System.getenv("CLASSPATH");
        if (cp == null || cp.isEmpty()) cp = System.getProperty("java.class.path", "");
        if (cp != null && !cp.isEmpty()) pb.environment().put("CLASSPATH", cp);
        java.lang.Process broker=pb.start();
        try(BufferedReader in=new BufferedReader(new InputStreamReader(broker.getInputStream(),StandardCharsets.UTF_8));BufferedWriter out=new BufferedWriter(new OutputStreamWriter(broker.getOutputStream(),StandardCharsets.UTF_8))){
            out.write(new JSONObject().put("op","paste").put("text",text).toString()+"\n");out.flush();line(in);
            try{
                if (DisplayInputHelper.injectKey(displayId, android.view.KeyEvent.KEYCODE_PASTE)) {
                    try{Thread.sleep(60L);}catch(InterruptedException ignored){}
                } else {
                    DisplayInputHelper.checkedInputShell("input","-d",String.valueOf(displayId),"keyevent","279");
                    try{Thread.sleep(240L);}catch(InterruptedException ignored){}
                }
            }
            finally{out.write("restore\n");out.flush();line(in);}
        }finally{if(broker.isAlive())broker.destroy();}
    }

    String getText() throws Exception {
        if (!isPrivilegedUid()) throw new SecurityException("只允许特权会话读取剪贴板");
        ProcessBuilder pb = new ProcessBuilder("/system/bin/app_process", "/system/bin", "dev.bubuki.proactive.RootClipboardBroker").redirectError(new File("/dev/null"));
        String cp = System.getenv("CLASSPATH");
        if (cp == null || cp.isEmpty()) cp = System.getProperty("java.class.path", "");
        if (cp != null && !cp.isEmpty()) pb.environment().put("CLASSPATH", cp);
        java.lang.Process broker = pb.start();
        try (BufferedReader in = new BufferedReader(new InputStreamReader(broker.getInputStream(), StandardCharsets.UTF_8));
             BufferedWriter out = new BufferedWriter(new OutputStreamWriter(broker.getOutputStream(), StandardCharsets.UTF_8))) {
            out.write(new JSONObject().put("op", "get").toString() + "\n");
            out.flush();
            String reply = line(in);
            return new JSONObject(reply).optString("text", "");
        } finally {
            if (broker.isAlive()) broker.destroy();
        }
    }

    void setText(String text) throws Exception {
        if (!isPrivilegedUid()) throw new SecurityException("只允许特权会话写入剪贴板");
        ProcessBuilder pb = new ProcessBuilder("/system/bin/app_process", "/system/bin", "dev.bubuki.proactive.RootClipboardBroker").redirectError(new File("/dev/null"));
        String cp = System.getenv("CLASSPATH");
        if (cp == null || cp.isEmpty()) cp = System.getProperty("java.class.path", "");
        if (cp != null && !cp.isEmpty()) pb.environment().put("CLASSPATH", cp);
        java.lang.Process broker = pb.start();
        try (BufferedReader in = new BufferedReader(new InputStreamReader(broker.getInputStream(), StandardCharsets.UTF_8));
             BufferedWriter out = new BufferedWriter(new OutputStreamWriter(broker.getOutputStream(), StandardCharsets.UTF_8))) {
            out.write(new JSONObject().put("op", "set").put("text", text != null ? text : "").toString() + "\n");
            out.flush();
            line(in);
        } finally {
            if (broker.isAlive()) broker.destroy();
        }
    }
}
