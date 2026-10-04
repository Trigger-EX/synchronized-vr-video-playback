// Native VR application: owns the render thread, the VrApi session and everything drawn.
//
// Threading: the JNI entry points (Activity lifecycle, surface callbacks) run on the Java main
// thread and only touch the mutex protected state below. Everything else, including every VrApi
// and GL call, happens on the render thread started by Start().
#pragma once

#include <EGL/egl.h>
#include <android/native_window.h>
#include <jni.h>

#include <atomic>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>

#include "VrApi.h"
#include "VrApi_Types.h"
#include "external_texture.h"
#include "sphere_renderer.h"

namespace syncvr {

class App {
public:
    App(JNIEnv* env, jobject activity);
    ~App();
    App(const App&) = delete;
    App& operator=(const App&) = delete;

    bool Start();
    // Stops the render thread (leaving VR mode, destroying swapchains) and drops the activity
    // reference. The object must be deleted afterwards.
    void Destroy(JNIEnv* env);

    // Activity lifecycle. OnPause and OnSurfaceDestroyed block (bounded) until the render thread
    // has left VR mode, as VrApi requires before the window goes away.
    void OnResume();
    void OnPause();
    void OnSurfaceChanged(JNIEnv* env, jobject surface);
    void OnSurfaceDestroyed();

    // mode: Mode (modes.h); stereo: Stereo; half180: the video is a 180-degree half-sphere.
    void SetMode(int mode, int stereo, bool half180);
    void SetVideoAspect(float aspect);
    // Operator "recenter": the next frame's head direction becomes the front of all content.
    void Recenter();
    // Larger panel for operator messages.
    void SetPanelProminent(bool prominent);
    std::string GetStatus();

private:
    void ThreadMain();

    bool InitVrApi();
    bool InitEgl();
    bool InitGl(JNIEnv* env);
    void LogDiagnostics();
    void NotifySurfacesReady(JNIEnv* env);
    void ShutdownGl(JNIEnv* env);
    void ShutdownEgl();

    bool EnterVr(ANativeWindow* window);
    void LeaveVr();
    void RunFrame(JNIEnv* env);
    void UpdateFps(int mode);
    void SetStatus(const std::string& status);
    bool WaitUntilOutOfVr(const char* why);

    // Set once at construction, read-only afterwards.
    JavaVM* vm_ = nullptr;
    jobject activity_ = nullptr;  // global reference

    // Lifecycle state shared with the Java main thread.
    std::mutex mutex_;
    std::condition_variable cv_;
    bool resumed_ = false;
    bool quit_ = false;
    bool inVr_ = false;  // true from just before vrapi_EnterVrMode until after LeaveVrMode
    ANativeWindow* window_ = nullptr;  // main-thread reference, guarded by mutex_

    std::atomic<int> mode_{0};
    std::atomic<int> stereo_{0};
    std::atomic<bool> half180_{false};
    std::atomic<float> videoAspect_{16.0f / 9.0f};
    std::atomic<bool> recenterRequested_{false};
    std::atomic<bool> panelProminent_{false};
    float yaw_ = 0.0f;  // render thread only

    std::mutex statusMutex_;
    std::string status_ = "starting";

    std::thread thread_;
    bool started_ = false;

    // Render thread only from here on.
    ovrJava java_{};
    ovrMobile* ovr_ = nullptr;
    ANativeWindow* activeWindow_ = nullptr;  // reference held while in VR mode
    bool vrApiInitialized_ = false;

    EGLDisplay display_ = EGL_NO_DISPLAY;
    EGLContext context_ = EGL_NO_CONTEXT;
    EGLSurface tinySurface_ = EGL_NO_SURFACE;
    EGLConfig config_ = nullptr;

    ovrTextureSwapChain* videoChain_ = nullptr;
    ovrTextureSwapChain* panelChain_ = nullptr;
    jobject videoSurface_ = nullptr;  // global references to the swapchain surfaces
    jobject panelSurface_ = nullptr;
    ExternalSurface external_;
    SphereRenderer sphere_;
    float texMatrix_[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

    long long frameIndex_ = 0;
    int lastMode_ = -1;
    double fpsWindowStart_ = 0.0;
    int fpsFrames_ = 0;
    ovrResult lastSubmitResult_ = 0;
    bool warnedSphere_ = false;
};

}  // namespace syncvr
