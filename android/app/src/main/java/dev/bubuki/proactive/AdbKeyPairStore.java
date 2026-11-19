package dev.bubuki.proactive;

import android.content.Context;
import org.bouncycastle.asn1.x500.X500Name;
import org.bouncycastle.cert.X509CertificateHolder;
import org.bouncycastle.cert.jcajce.JcaX509CertificateConverter;
import org.bouncycastle.cert.jcajce.JcaX509v3CertificateBuilder;
import org.bouncycastle.jce.provider.BouncyCastleProvider;
import org.bouncycastle.operator.ContentSigner;
import org.bouncycastle.operator.jcajce.JcaContentSignerBuilder;
import java.io.ByteArrayInputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.math.BigInteger;
import java.security.KeyFactory;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.PrivateKey;
import java.security.SecureRandom;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.security.spec.PKCS8EncodedKeySpec;
import java.util.Date;

/** Persistent 2048-bit RSA keypair & self-signed X.509 certificate for Android 11+ Wireless ADB TLS. */
final class AdbKeyPairStore {
    static final class KeyMaterial {
        final PrivateKey privateKey;
        final X509Certificate certificate;
        KeyMaterial(PrivateKey privateKey, X509Certificate certificate) {
            this.privateKey = privateKey;
            this.certificate = certificate;
        }
    }

    private AdbKeyPairStore() {}

    static synchronized KeyMaterial loadOrCreate(Context context) throws Exception {
        File dir = new File(context.getFilesDir(), "adb_key");
        if (!dir.exists()) {
            //noinspection ResultOfMethodCallIgnored
            dir.mkdirs();
        }
        File keyFile = new File(dir, "private.pk8");
        File certFile = new File(dir, "cert.der");
        if (keyFile.exists() && certFile.exists() && keyFile.length() > 32 && certFile.length() > 32) {
            try {
                byte[] pkcs8 = readAll(keyFile);
                byte[] certDer = readAll(certFile);
                PrivateKey pk = KeyFactory.getInstance("RSA").generatePrivate(new PKCS8EncodedKeySpec(pkcs8));
                X509Certificate cert = (X509Certificate) CertificateFactory.getInstance("X.509")
                    .generateCertificate(new ByteArrayInputStream(certDer));
                return new KeyMaterial(pk, cert);
            } catch (Throwable ignored) {
                // Corrupted files: regenerate fresh keypair below
            }
        }

        KeyPairGenerator kpg = KeyPairGenerator.getInstance("RSA");
        kpg.initialize(2048, new SecureRandom());
        KeyPair kp = kpg.generateKeyPair();

        long now = System.currentTimeMillis();
        Date notBefore = new Date(now - 86_400_000L);
        Date notAfter = new Date(now + 10L * 365L * 86_400_000L);
        X500Name owner = new X500Name("CN=ProactiveAgent-WhaleChan");
        BigInteger serial = new BigInteger(64, new SecureRandom());
        JcaX509v3CertificateBuilder certBuilder = new JcaX509v3CertificateBuilder(
            owner,
            serial,
            notBefore,
            notAfter,
            owner,
            kp.getPublic()
        );
        ContentSigner signer = new JcaContentSignerBuilder("SHA256WithRSAEncryption")
            .setProvider(new BouncyCastleProvider())
            .build(kp.getPrivate());
        X509CertificateHolder holder = certBuilder.build(signer);
        X509Certificate cert = new JcaX509CertificateConverter()
            .setProvider(new BouncyCastleProvider())
            .getCertificate(holder);

        writeBytes(keyFile, kp.getPrivate().getEncoded());
        writeBytes(certFile, cert.getEncoded());
        return new KeyMaterial(kp.getPrivate(), cert);
    }

    private static byte[] readAll(File f) throws Exception {
        byte[] buf = new byte[(int) f.length()];
        try (FileInputStream fis = new FileInputStream(f)) {
            int off = 0;
            while (off < buf.length) {
                int r = fis.read(buf, off, buf.length - off);
                if (r < 0) break;
                off += r;
            }
        }
        return buf;
    }

    private static void writeBytes(File f, byte[] data) throws Exception {
        try (FileOutputStream fos = new FileOutputStream(f)) {
            fos.write(data);
        }
    }
}
