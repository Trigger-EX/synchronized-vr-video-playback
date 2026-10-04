import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (CI decodes the keystore from GitHub secrets);
// the same key signs the player and the operator app.
val keystoreFile: String? = System.getenv("SYNCVR_KEYSTORE_FILE")

android {
    namespace = "com.syncvr.operator"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.syncvr.operator"
        minSdk = 24
        targetSdk = 34
        // CI run number keeps versionCode increasing so updates install over older builds.
        versionCode = System.getenv("GITHUB_RUN_NUMBER")?.toInt() ?: 1
        versionName = "0.1.0"
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
            signingConfig = signingConfigs.findByName("release")
        }
    }

    // Release lint needs network access for its own tooling; CI builds the APK only.
    lint { checkReleaseBuilds = false }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

dependencies {
    // Framework widgets only; all logic lives in :core (com.syncvr.player.core.operator).
    implementation(project(":core"))
}
