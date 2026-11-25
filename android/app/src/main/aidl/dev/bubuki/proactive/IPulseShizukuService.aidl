package dev.bubuki.proactive;

import android.os.ParcelFileDescriptor;

interface IPulseShizukuService {
    void destroy() = 16777114;
    void exit() = 1;
    int getUid() = 2;
    void attachPipe(in ParcelFileDescriptor readFd, in ParcelFileDescriptor writeFd, String token, String apkPath) = 3;
}
