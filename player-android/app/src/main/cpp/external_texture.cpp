#include "external_texture.h"

#include <GLES2/gl2ext.h>

#include "gl_util.h"
#include "jni_util.h"
#include "log.h"

namespace syncvr {

bool ExternalSurface::Create(JNIEnv* env) {
    glGenTextures(1, &texture_);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture_);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);
    if (CheckGl("external texture setup") || texture_ == 0) return false;

    jclass stClass = env->FindClass("android/graphics/SurfaceTexture");
    if (stClass == nullptr || JniFailed(env, "FindClass SurfaceTexture")) return false;
    jclass surfaceClass = env->FindClass("android/view/Surface");
    if (surfaceClass == nullptr || JniFailed(env, "FindClass Surface")) return false;

    jmethodID stCtor = env->GetMethodID(stClass, "<init>", "(I)V");
    updateTexImage_ = env->GetMethodID(stClass, "updateTexImage", "()V");
    getTransformMatrix_ = env->GetMethodID(stClass, "getTransformMatrix", "([F)V");
    releaseTexture_ = env->GetMethodID(stClass, "release", "()V");
    jmethodID surfaceCtor = env->GetMethodID(surfaceClass, "<init>", "(Landroid/graphics/SurfaceTexture;)V");
    releaseSurface_ = env->GetMethodID(surfaceClass, "release", "()V");
    if (JniFailed(env, "SurfaceTexture/Surface method lookup")) return false;
    if (!stCtor || !updateTexImage_ || !getTransformMatrix_ || !releaseTexture_ || !surfaceCtor ||
        !releaseSurface_) {
        LOGE("SurfaceTexture/Surface method lookup returned null");
        return false;
    }

    jobject st = env->NewObject(stClass, stCtor, static_cast<jint>(texture_));
    if (st == nullptr || JniFailed(env, "new SurfaceTexture")) return false;
    surfaceTexture_ = env->NewGlobalRef(st);
    env->DeleteLocalRef(st);

    jobject surface = env->NewObject(surfaceClass, surfaceCtor, surfaceTexture_);
    if (surface == nullptr || JniFailed(env, "new Surface(SurfaceTexture)")) return false;
    surface_ = env->NewGlobalRef(surface);
    env->DeleteLocalRef(surface);

    jfloatArray arr = env->NewFloatArray(16);
    if (arr == nullptr || JniFailed(env, "NewFloatArray")) return false;
    matrixArray_ = static_cast<jfloatArray>(env->NewGlobalRef(arr));
    env->DeleteLocalRef(arr);

    env->DeleteLocalRef(stClass);
    env->DeleteLocalRef(surfaceClass);
    LOGI("External OES texture %u with SurfaceTexture ready", static_cast<unsigned>(texture_));
    return true;
}

void ExternalSurface::Destroy(JNIEnv* env) {
    if (surface_ != nullptr) {
        env->CallVoidMethod(surface_, releaseSurface_);
        JniFailed(env, "Surface.release");
        env->DeleteGlobalRef(surface_);
        surface_ = nullptr;
    }
    if (surfaceTexture_ != nullptr) {
        env->CallVoidMethod(surfaceTexture_, releaseTexture_);
        JniFailed(env, "SurfaceTexture.release");
        env->DeleteGlobalRef(surfaceTexture_);
        surfaceTexture_ = nullptr;
    }
    if (matrixArray_ != nullptr) {
        env->DeleteGlobalRef(matrixArray_);
        matrixArray_ = nullptr;
    }
    if (texture_ != 0) {
        glDeleteTextures(1, &texture_);
        texture_ = 0;
    }
}

bool ExternalSurface::Update(JNIEnv* env, float* matrix16) {
    if (surfaceTexture_ == nullptr) return false;
    env->CallVoidMethod(surfaceTexture_, updateTexImage_);
    if (JniFailed(env, "SurfaceTexture.updateTexImage")) return false;
    env->CallVoidMethod(surfaceTexture_, getTransformMatrix_, matrixArray_);
    if (JniFailed(env, "SurfaceTexture.getTransformMatrix")) return false;
    env->GetFloatArrayRegion(matrixArray_, 0, 16, matrix16);
    return !JniFailed(env, "GetFloatArrayRegion");
}

}  // namespace syncvr
