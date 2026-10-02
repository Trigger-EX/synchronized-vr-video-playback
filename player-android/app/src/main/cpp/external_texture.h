// A SurfaceTexture bound to a GL_TEXTURE_EXTERNAL_OES texture, plus the Surface built from it.
// Used by the sphere fallback: ExoPlayer decodes into the Surface, the render thread latches
// frames with updateTexImage() and samples the texture. All methods must be called on the render
// thread with the GL context current.
#pragma once

#include <GLES3/gl3.h>
#include <jni.h>

namespace syncvr {

class ExternalSurface {
public:
    ExternalSurface() = default;
    ExternalSurface(const ExternalSurface&) = delete;
    ExternalSurface& operator=(const ExternalSurface&) = delete;

    bool Create(JNIEnv* env);
    void Destroy(JNIEnv* env);

    // Latches the newest frame and returns its texture transform (column-major 4x4).
    bool Update(JNIEnv* env, float* matrix16);

    GLuint texture() const { return texture_; }
    jobject surface() const { return surface_; }  // global reference
    bool valid() const { return surface_ != nullptr; }

private:
    GLuint texture_ = 0;
    jobject surfaceTexture_ = nullptr;  // global reference
    jobject surface_ = nullptr;         // global reference
    jfloatArray matrixArray_ = nullptr;  // global reference
    jmethodID updateTexImage_ = nullptr;
    jmethodID getTransformMatrix_ = nullptr;
    jmethodID releaseTexture_ = nullptr;
    jmethodID releaseSurface_ = nullptr;
};

}  // namespace syncvr
