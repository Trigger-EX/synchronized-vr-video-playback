plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (CI decodes the SYNCVR_KEYSTORE_* secrets).
// Without it, release builds stay unsigned; CI refuses to continue in that case.
val keystoreFile: String? = System.getenv("SYNCVR_KEYSTORE_FILE")

android {
    namespace = "com.syncvr.player"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.syncvr.player"
        minSdk = 25 // Oculus Go runs Android 7.1
        targetSdk = 25
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "0.1.0"
        ndk {
            abiFilters += "armeabi-v7a"
        }
    }

    signingConfigs {
        if (keystoreFile != null) {
            create("release") {
                storeFile = file(keystoreFile)
                storePassword = System.getenv("SYNCVR_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("SYNCVR_KEY_ALIAS")
                keyPassword = System.getenv("SYNCVR_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (keystoreFile != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        // targetSdk matches the Go's Android version on purpose; the app is sideloaded.
        disable += "ExpiredTargetSdkVersion"
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(project(":core"))
}
