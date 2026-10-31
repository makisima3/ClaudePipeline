"""Сборка автономного APK стандартными JDK/Android SDK, без Gradle, npm и pip."""
import argparse
import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", default=os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT"))
    parser.add_argument("--java", default=os.environ.get("JAVA_HOME"))
    parser.add_argument("--platform", default="36")
    parser.add_argument("--build-tools", default="36.0.0")
    args = parser.parse_args()
    if not args.sdk or not args.java:
        parser.error("Укажите --sdk и --java либо ANDROID_HOME и JAVA_HOME (JDK 17+).")
    sdk, jdk = Path(args.sdk), Path(args.java)
    bt = sdk / "build-tools" / args.build_tools
    android = sdk / "platforms" / ("android-" + args.platform) / "android.jar"
    build, local = ROOT / "build", ROOT / ".local"
    if not build.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Каталог сборки должен оставаться внутри android/.")
    for name in ("gen", "classes", "dex"):
        intermediate = build / name
        if not intermediate.resolve().is_relative_to(build.resolve()):
            raise ValueError("Промежуточный каталог выходит за android/build/.")
        if intermediate.exists():
            shutil.rmtree(intermediate)
    for p in (build / "gen", build / "classes", build / "dex", local):
        p.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "JAVA_HOME": str(jdk)}
    suffix = ".exe" if os.name == "nt" else ""

    def run(argv):
        # Не печатаем аргументы: в командах подписи есть локальный пароль тестового ключа.
        subprocess.run([str(a) for a in argv], env=env, check=True)

    java = jdk / "bin" / ("java" + suffix)
    aapt = bt / ("aapt2" + suffix)
    compiled = build / "resources.zip"
    run([aapt, "compile", "--dir", ROOT / "res", "-o", compiled])
    base = build / "base.apk"
    run([aapt, "link", "-I", android, "--manifest", ROOT / "AndroidManifest.xml", "--java", build / "gen", "-o", base, compiled])
    sources = [*sorted((ROOT / "src").rglob("*.java")), *sorted((build / "gen").rglob("*.java"))]
    run([jdk / "bin" / ("javac" + suffix), "-encoding", "UTF-8", "--release", "8", "-classpath", android, "-d", build / "classes", *sources])
    classes = build / "classes.jar"
    with zipfile.ZipFile(classes, "w") as z:
        for f in sorted((build / "classes").rglob("*.class")):
            z.write(f, f.relative_to(build / "classes").as_posix())
    run([java, "-cp", bt / "lib/d8.jar", "com.android.tools.r8.D8", "--min-api", "26", "--lib", android, "--output", build / "dex", classes])
    unsigned = build / "unsigned.apk"
    shutil.copyfile(base, unsigned)
    with zipfile.ZipFile(unsigned, "a") as z:
        z.write(build / "dex/classes.dex", "classes.dex")
    aligned = build / "aligned.apk"
    run([bt / ("zipalign" + suffix), "-f", "4", unsigned, aligned])
    key = local / "signing.jks"
    if not key.exists():
        run([jdk / "bin" / ("keytool" + suffix), "-genkeypair", "-keystore", key, "-storepass", "android", "-keypass", "android",
             "-alias", "agentpipeline", "-dname", "CN=AgentPipeline Personal Build", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000"])
    apk = build / "AgentPipeline.apk"
    signer = [java, "-jar", bt / "lib/apksigner.jar"]
    run([*signer, "sign", "--ks", key, "--ks-key-alias", "agentpipeline", "--ks-pass", "pass:android", "--key-pass", "pass:android", "--out", apk, aligned])
    run([*signer, "verify", "--verbose", apk])
    run([bt / ("zipalign" + suffix), "-c", "4", apk])
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    (build / "AgentPipeline.apk.sha256").write_text(digest + "  AgentPipeline.apk\n", encoding="ascii")
    print(f"APK: {apk}\nSHA-256: {digest}")


if __name__ == "__main__":
    main()
