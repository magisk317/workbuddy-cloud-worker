# -*- coding: utf-8 -*-
"""WB Monitor APK 构建（Linux/debian 原生，零第三方依赖）

步骤：aapt2 compile -> aapt2 link -> javac -> R8/D8 -> 注入 dex -> zipalign -> apksigner。
默认工具链与 xinyi-relay、XposedSmsCode、MiPushFramework 对齐：Java 27、
compile/target SDK 37、min SDK 37。R8 优先使用 Android SDK cmdline-tools
附带的新版本；可通过环境变量覆盖所有路径。

用法：
    python3 build_apk.py
    python3 build_apk.py --clean
"""
import argparse
import glob
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = os.name == "nt"
TOOL_EXT = ".exe" if IS_WINDOWS else ""
SDK = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or (
    r"C:\\Android" if IS_WINDOWS else "/opt/android-sdk")
JDK = os.environ.get("JAVA_HOME") or (
    os.path.join(SDK, "jdk17") if IS_WINDOWS else "/home/linuxbrew/.linuxbrew/opt/openjdk")
JAVAC = os.path.join(JDK, "bin", "javac" + TOOL_EXT)
if not os.path.exists(JAVAC):
    _javac_candidates = glob.glob(os.path.join(SDK, "jdk*", "**", "bin", "javac" + TOOL_EXT), recursive=True)
    if _javac_candidates:
        JDK = os.path.dirname(os.path.dirname(_javac_candidates[0]))
        JAVAC = os.path.join(JDK, "bin", "javac" + TOOL_EXT)
if os.path.isdir(os.path.join(SDK, "build-tools")):
    _bt_versions = sorted(glob.glob(os.path.join(SDK, "build-tools", "*")), reverse=True)
else:
    _bt_versions = []
BT = os.environ.get("ANDROID_BUILD_TOOLS") or (_bt_versions[0] if _bt_versions else "")
AJ = os.environ.get("ANDROID_JAR") or os.path.join(SDK, "platforms", "android-37.2", "android.jar")
R8_JAR = os.environ.get("R8_JAR") or os.path.join(SDK, "cmdline-tools", "latest", "lib", "r8.jar")
KS = os.path.join(HERE, "release.jks")
SHARED_CERT_SHA256 = "027b936e1d82a606d83599548b8b11e49a2c8ae2588c4a9fbe8311e2c54f103a"
KS_CANDIDATES = (
    KS,
    "/root/play/xinyi-relay/release.jks",
    "/root/play/XposedSmsCode/release.jks",
    "/root/play/push/MiPushFramework/release.jks",
)
PROPS_CANDIDATES = (
    os.path.join(HERE, "signature.properties"),
    "/root/play/xinyi-relay/signature.properties",
    "/root/play/XposedSmsCode/app/signature.properties",
    "/root/play/push/MiPushFramework/signature.properties",
)
JAVA_RELEASE = os.environ.get("WB_JAVA_RELEASE") or ("17" if IS_WINDOWS else "27")
MIN_SDK = os.environ.get("WB_MIN_SDK") or "37"
TARGET_SDK = os.environ.get("WB_TARGET_SDK") or "37"


def resolve_signing():
    ks = os.environ.get("WB_KEYSTORE") or next((p for p in KS_CANDIDATES if os.path.exists(p)), "")
    props = os.environ.get("WB_SIGNING_PROPERTIES") or next(
        (p for p in PROPS_CANDIDATES if os.path.exists(p)), "")
    values = {}
    if props:
        with open(props, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip()
    store = os.environ.get("STORE_PASSWORD") or values.get("STORE_PASSWORD", "")
    alias = os.environ.get("KEY_ALIAS") or values.get("KEY_ALIAS", "")
    key_pass = os.environ.get("KEY_PASSWORD") or values.get("KEY_PASSWORD", "")
    missing = [name for name, value in (("keystore", ks), ("STORE_PASSWORD", store),
                                       ("KEY_ALIAS", alias), ("KEY_PASSWORD", key_pass)) if not value]
    if missing:
        raise SystemExit("[签名缺失] %s（组内共享签名：xinyi/XSC/MiPush 的 release.jks）"
                         % ", ".join(missing))
    return ks, store, alias, key_pass


def tool(name):
    return os.path.join(BT, name + TOOL_EXT)


def java_tool(name):
    return os.path.join(JDK, "bin", name + TOOL_EXT)


def run(cmd, cwd=HERE, label=""):
    print("  > %s" % (label or " ".join(os.path.basename(c) if i == 0 else c
                                       for i, c in enumerate(cmd))[:150]))
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if p.returncode != 0:
        print(out[-2500:])
        raise SystemExit("[失败] %s rc=%s" % (label or cmd[0], p.returncode))
    if out:
        print("    " + out[-600:].replace("\n", "\n    "))
    return out


def class_files():
    return [p.replace("\\", "/") for p in glob.glob(os.path.join(HERE, "obj", "**", "*.class"),
                                                    recursive=True)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    a = ap.parse_args()

    hits = glob.glob(os.path.join(HERE, "java", "**", "LocalConfig.java"), recursive=True)
    if not hits:
        raise SystemExit("[未配置] 请先在仓库根目录运行: python3 scripts/configure.py")

    ks, store_pass, key_alias, key_pass = resolve_signing()
    d8_cp = R8_JAR if os.path.exists(R8_JAR) else os.path.join(BT, "lib", "d8.jar")

    for p in (("gen", "obj", "dex", "out") if a.clean else ()):
        shutil.rmtree(os.path.join(HERE, p), ignore_errors=True)
    for p in ("gen", "obj", "dex", "out"):
        os.makedirs(os.path.join(HERE, p), exist_ok=True)

    for need in (JAVAC, tool("aapt2"), tool("zipalign"),
                 java_tool("java"), os.path.join(BT, "lib", "apksigner.jar"), AJ, d8_cp):
        if not os.path.exists(need):
            raise SystemExit("[缺失] %s" % need)

    print("[1/7] aapt2 compile 资源")
    run([tool("aapt2"), "compile", "--dir", "res", "-o", "res.zip"])

    print("[2/7] aapt2 link")
    run([tool("aapt2"), "link", "-I", AJ,
         "--manifest", "AndroidManifest.xml", "-o", "out/base.apk",
         "--java", "gen", "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
         "--auto-add-overlay", "res.zip"])

    print("[3/7] javac (--release %s)" % JAVA_RELEASE)
    srcs = []
    for d in ("java", "gen"):
        srcs += [p.replace("\\", "/") for p in glob.glob(os.path.join(HERE, d, "**", "*.java"),
                                                         recursive=True)]
    with open(os.path.join(HERE, "sources.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(os.path.relpath(s, HERE).replace("\\", "/") for s in srcs))
    run([JAVAC, "--release", JAVA_RELEASE, "-nowarn", "-encoding", "UTF-8",
         "-J-Duser.language=en", "-J-Duser.country=US",
         "-classpath", AJ, "-d", "obj", "@sources.txt"])

    print("[4/7] R8/D8 转 dex（Java %s）" % JAVA_RELEASE)
    run([java_tool("java"), "-cp", d8_cp,
         "com.android.tools.r8.D8", "--lib", AJ, "--output", "dex"] + class_files())

    print("[5/7] 注入 dex 并对齐")
    shutil.copy(os.path.join(HERE, "out", "base.apk"), os.path.join(HERE, "out", "unsigned.apk"))
    z = zipfile.ZipFile(os.path.join(HERE, "out", "unsigned.apk"), "a", zipfile.ZIP_DEFLATED)
    z.write(os.path.join(HERE, "dex", "classes.dex"), "classes.dex")
    z.close()
    run([tool("zipalign"), "-f", "-p", "4", "out/unsigned.apk", "out/aligned.apk"])

    print("[6/7] 签名（组内共享 magisk317 release key）")
    run([java_tool("java"), "-jar", os.path.join(BT, "lib", "apksigner.jar"),
         "sign", "--ks", ks, "--ks-key-alias", key_alias,
         "--ks-pass", "pass:" + store_pass, "--key-pass", "pass:" + key_pass,
         "--out", "out/wbmon.apk", "out/aligned.apk"], label="sign APK")

    print("[7/7] 校验签名与证书指纹")
    certs = run([java_tool("java"), "-jar", os.path.join(BT, "lib", "apksigner.jar"),
                 "verify", "--print-certs", "out/wbmon.apk"])
    if SHARED_CERT_SHA256 not in certs.lower():
        raise SystemExit("[签名不符] 期望组内证书 %s" % SHARED_CERT_SHA256)
    run([java_tool("java"), "-jar", os.path.join(BT, "lib", "apksigner.jar"),
         "verify", "--verbose", "out/wbmon.apk"])

    apk = os.path.join(HERE, "out", "wbmon.apk")
    payload = open(apk, "rb").read()
    print("\n=== 构建完成 ===")
    print("  APK : %s" % apk)
    print("  大小: %d 字节" % os.path.getsize(apk))
    print("  MD5 : %s" % hashlib.md5(payload).hexdigest())
    print("  SHA-256: %s" % hashlib.sha256(payload).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
