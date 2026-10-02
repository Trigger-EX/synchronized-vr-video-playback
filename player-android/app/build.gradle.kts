import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing comes from the environment (CI decodes the keystore from GitHub secrets).
val keystoreFile: String? = System.getenv("SYNCVR_KEYSTORE_FILE")

android {
    namespace = "com.syncvr.player"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.syncvr.player"
        minSdk = 25 // Oculus Go runs Android 7.1
        targetSdk = 34
        // CI run number keeps versionCode increasing so updates install over older builds.
        versionCode = System.getenv("GITHUB_RUN_NUMBER")?.toInt() ?: 1
        versionName = "0.1.0"
        ndk { abiFilters += "armeabi-v7a" }
    }

    buildFeatures { buildConfig = true }

    // C++ VR loop; needs the VrApi SDK from tools/fetch-vrapi.sh.
    externalNativeBuild {
        cmake { path = file("src/main/cpp/CMakeLists.txt") }
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
    implementation(project(":core"))
    implementation("androidx.media3:media3-exoplayer:1.4.1")
}
