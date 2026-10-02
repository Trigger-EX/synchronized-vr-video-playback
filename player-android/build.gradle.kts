// Plugins go on the classpath here instead of a plugins {} block so the Android Gradle Plugin is
// only fetched when :app is included (it needs Google's Maven, which some networks can't reach).
buildscript {
    repositories {
        google()
        mavenCentral()
    }
    dependencies {
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:2.0.21")
        if (findProject(":app") != null) classpath("com.android.tools.build:gradle:8.7.3")
    }
}
