// JNI entry points for com.syncvr.player.NativeBridge. The handle is an App* stored in a jlong.
#include <jni.h>

#include "VrApi.h"
#include "app.h"
#include "log.h"

namespace {

syncvr::App* FromHandle(jlong handle) {
    return reinterpret_cast<syncvr::App*>(handle);
}

}  // namespace

extern "C" {

JNIEXPORT jlong JNICALL Java_com_syncvr_player_NativeBridge_nativeCreate(JNIEnv* env, jclass,
                                                                         jobject activity) {
    syncvr::App* app = new syncvr::App(env, activity);
    if (!app->Start()) {
        LOGE("App::Start failed");
        app->Destroy(env);
        delete app;
        return 0;
    }
    return reinterpret_cast<jlong>(app);
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeDestroy(JNIEnv* env, jclass,
                                                                         jlong handle) {
    syncvr::App* app = FromHandle(handle);
    if (app == nullptr) return;
    app->Destroy(env);
    delete app;
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeResume(JNIEnv*, jclass,
                                                                        jlong handle) {
    if (syncvr::App* app = FromHandle(handle)) app->OnResume();
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativePause(JNIEnv*, jclass,
                                                                       jlong handle) {
    if (syncvr::App* app = FromHandle(handle)) app->OnPause();
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeSurfaceChanged(
    JNIEnv* env, jclass, jlong handle, jobject surface) {
    if (syncvr::App* app = FromHandle(handle)) app->OnSurfaceChanged(env, surface);
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeSurfaceDestroyed(JNIEnv*, jclass,
                                                                                  jlong handle) {
    if (syncvr::App* app = FromHandle(handle)) app->OnSurfaceDestroyed();
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeSetMode(JNIEnv*, jclass,
                                                                         jlong handle, jint mode,
                                                                         jint stereo,
                                                                         jboolean half180) {
    if (syncvr::App* app = FromHandle(handle)) {
        app->SetMode(static_cast<int>(mode), static_cast<int>(stereo), half180 == JNI_TRUE);
    }
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeSetVideoAspect(JNIEnv*, jclass,
                                                                                jlong handle,
                                                                                jfloat aspect) {
    if (syncvr::App* app = FromHandle(handle)) app->SetVideoAspect(static_cast<float>(aspect));
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeRecenter(JNIEnv*, jclass,
                                                                          jlong handle) {
    if (syncvr::App* app = FromHandle(handle)) app->Recenter();
}

JNIEXPORT jfloatArray JNICALL Java_com_syncvr_player_NativeBridge_nativeGetPose(JNIEnv* env, jclass,
                                                                               jlong handle) {
    float pose[3] = {0.0f, 0.0f, 0.0f};
    if (syncvr::App* app = FromHandle(handle)) app->GetPose(pose);
    jfloatArray out = env->NewFloatArray(3);
    if (out != nullptr) env->SetFloatArrayRegion(out, 0, 3, pose);
    return out;
}

JNIEXPORT void JNICALL Java_com_syncvr_player_NativeBridge_nativeSetPanelProminent(
    JNIEnv*, jclass, jlong handle, jboolean prominent) {
    if (syncvr::App* app = FromHandle(handle)) app->SetPanelProminent(prominent == JNI_TRUE);
}

JNIEXPORT jstring JNICALL Java_com_syncvr_player_NativeBridge_nativeGetStatus(JNIEnv* env, jclass,
                                                                              jlong handle) {
    syncvr::App* app = FromHandle(handle);
    return env->NewStringUTF(app == nullptr ? "no native app" : app->GetStatus().c_str());
}

}  // extern "C"
