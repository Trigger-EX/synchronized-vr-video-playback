#include "app.h"

#include <GLES3/gl3.h>
#include <android/native_window_jni.h>
#include <stdio.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

#include <chrono>
#include <vector>

#include "VrApi_Helpers.h"
#include "VrApi_Types.h"
#include "gl_util.h"
#include "jni_util.h"
#include "layers.h"
#include "log.h"
#include "modes.h"

#ifndef EGL_OPENGL_ES3_BIT_KHR
#define EGL_OPENGL_ES3_BIT_KHR 0x0040
#endif

namespace syncvr {

namespace {

const int kVideoSwapChainSize = 2048;
const int kPanelWidth = 1024;
const int kPanelHeight = 256;
const int kCpuLevel = 2;
const int kGpuLevel = 3;
const double kFpsLogIntervalSeconds = 5.0;
const int kLifecycleWaitMs = 3000;

uint32_t CurrentThreadId() {
    return static_cast<uint32_t>(syscall(SYS_gettid));
}

}  // namespace

App::App(JNIEnv* env, jobject activity) {
    env->GetJavaVM(&vm_);
    activity_ = env->NewGlobalRef(activity);
}

App::~App() = default;

bool App::Start() {
    if (vm_ == nullptr || activity_ == nullptr) {
        LOGE("App::Start without JavaVM or activity");
        return false;
    }
    thread_ = std::thread(&App::ThreadMain, this);
    started_ = true;
    return true;
}

void App::Destroy(JNIEnv* env) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        quit_ = true;
        resumed_ = false;
    }
    cv_.notify_all();
    if (started_ && thread_.joinable()) thread_.join();
    started_ = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (window_ != nullptr) {
            ANativeWindow_release(window_);
            window_ = nullptr;
        }
    }
    if (activity_ != nullptr) {
        env->DeleteGlobalRef(activity_);
        activity_ = nullptr;
    }
}

// ---------------------------------------------------------------------------------------------
// Lifecycle (Java main thread)
// ---------------------------------------------------------------------------------------------

bool App::WaitUntilOutOfVr(const char* why) {
    std::unique_lock<std::mutex> lock(mutex_);
    const bool ok = cv_.wait_for(
        lock, std::chrono::milliseconds(kLifecycleWaitMs), [this] { return !inVr_; });
    if (!ok) LOGW("Render thread still in VR mode %dms after %s", kLifecycleWaitMs, why);
    return ok;
}

void App::OnResume() {
    LOGI("lifecycle: resume");
    {
        std::lock_guard<std::mutex> lock(mutex_);
        resumed_ = true;
    }
    cv_.notify_all();
}

void App::OnPause() {
    LOGI("lifecycle: pause");
    {
        std::lock_guard<std::mutex> lock(mutex_);
        resumed_ = false;
    }
    cv_.notify_all();
    WaitUntilOutOfVr("pause");
}

void App::OnSurfaceChanged(JNIEnv* env, jobject surface) {
    ANativeWindow* window = ANativeWindow_fromSurface(env, surface);
    if (window == nullptr) {
        LOGE("ANativeWindow_fromSurface returned null");
        return;
    }
    bool changed = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (window == window_) {
            // Same window as before (surfaceChanged after surfaceCreated): drop the extra ref.
            ANativeWindow_release(window);
        } else {
            if (window_ != nullptr) ANativeWindow_release(window_);
            window_ = window;
            changed = true;
        }
    }
    LOGI("lifecycle: surface %s", changed ? "created/replaced" : "changed (same window)");
    if (changed) cv_.notify_all();
}

void App::OnSurfaceDestroyed() {
    LOGI("lifecycle: surface destroyed");
    ANativeWindow* old = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        old = window_;
        window_ = nullptr;
    }
    cv_.notify_all();
    WaitUntilOutOfVr("surface destruction");
    // The render thread holds its own reference while in VR mode.
    if (old != nullptr) ANativeWindow_release(old);
}

void App::SetMode(int mode) {
    if (mode < 0 || mode >= kModeCount) {
        LOGW("Ignoring invalid mode %d", mode);
        return;
    }
    mode_.store(mode);
}

void App::SetVideoAspect(float aspect) {
    if (aspect > 0.1f && aspect < 10.0f) videoAspect_.store(aspect);
}

std::string App::GetStatus() {
    std::lock_guard<std::mutex> lock(statusMutex_);
    return status_;
}

void App::SetStatus(const std::string& status) {
    std::lock_guard<std::mutex> lock(statusMutex_);
    status_ = status;
}

// ---------------------------------------------------------------------------------------------
// Render thread
// ---------------------------------------------------------------------------------------------

void App::ThreadMain() {
    JNIEnv* env = nullptr;
    if (vm_->AttachCurrentThread(&env, nullptr) != JNI_OK || env == nullptr) {
        LOGE("AttachCurrentThread failed for the render thread");
        SetStatus("render thread attach failed");
        return;
    }
    java_.Vm = vm_;
    java_.Env = env;
    java_.ActivityObject = activity_;

    const bool ok = InitVrApi() && InitEgl() && InitGl(env);
    if (!ok) {
        LOGE("Initialization failed, render thread idles until shutdown");
        SetStatus("VR init failed, see logcat");
    } else {
        LogDiagnostics();
        NotifySurfacesReady(env);
        SetStatus("waiting for VR mode");
    }

    bool enterFailed = false;
    while (true) {
        ANativeWindow* enterWindow = nullptr;
        bool leave = false;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            if (!ok || ovr_ == nullptr) {
                cv_.wait(lock, [this, ok] {
                    return quit_ || (ok && resumed_ && window_ != nullptr);
                });
            }
            if (quit_) break;
            const bool want = resumed_ && window_ != nullptr;
            if (ovr_ != nullptr) {
                if (!want || window_ != activeWindow_) leave = true;
            } else if (want && ok) {
                enterWindow = window_;
                ANativeWindow_acquire(enterWindow);
                inVr_ = true;
            }
        }
        if (leave) {
            LeaveVr();
        } else if (enterWindow != nullptr) {
            enterFailed = !EnterVr(enterWindow);
            if (enterFailed) {
                // Typically the buffer queue was abandoned; wait a moment for a new window.
                std::this_thread::sleep_for(std::chrono::milliseconds(500));
            }
        } else if (ovr_ != nullptr) {
            RunFrame(env);
        }
    }
    (void)enterFailed;

    if (ovr_ != nullptr) LeaveVr();
    ShutdownGl(env);
    ShutdownEgl();
    if (vrApiInitialized_) {
        vrapi_Shutdown();
        vrApiInitialized_ = false;
        LOGI("vrapi_Shutdown done");
    }
    vm_->DetachCurrentThread();
}

bool App::InitVrApi() {
    ovrInitParms initParms = vrapi_DefaultInitParms(&java_);
    initParms.GraphicsAPI = VRAPI_GRAPHICS_API_OPENGL_ES_3;
    const ovrInitializeStatus status = vrapi_Initialize(&initParms);
    if (status != VRAPI_INITIALIZE_SUCCESS) {
        LOGE("vrapi_Initialize failed: %d", static_cast<int>(status));
        return false;
    }
    vrApiInitialized_ = true;
    LOGI("vrapi_Initialize ok");
    return true;
}

bool App::InitEgl() {
    display_ = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (display_ == EGL_NO_DISPLAY) {
        LOGE("eglGetDisplay failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    EGLint major = 0;
    EGLint minor = 0;
    if (!eglInitialize(display_, &major, &minor)) {
        LOGE("eglInitialize failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    LOGI("EGL %d.%d", static_cast<int>(major), static_cast<int>(minor));

    EGLint numConfigs = 0;
    if (!eglGetConfigs(display_, nullptr, 0, &numConfigs) || numConfigs <= 0) {
        LOGE("eglGetConfigs found no configs: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    std::vector<EGLConfig> configs(static_cast<size_t>(numConfigs));
    if (!eglGetConfigs(display_, configs.data(), numConfigs, &numConfigs)) {
        LOGE("eglGetConfigs failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    config_ = nullptr;
    // First pass wants a plain RGBA8888 config without depth/stencil/MSAA, like the SDK samples;
    // the second pass accepts anything usable.
    for (int pass = 0; pass < 2 && config_ == nullptr; pass++) {
        for (EGLint i = 0; i < numConfigs; i++) {
            EGLint renderable = 0;
            EGLint surfaceType = 0;
            EGLint r = 0, g = 0, b = 0, a = 0, depth = 0, stencil = 0, samples = 0;
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_RENDERABLE_TYPE, &renderable);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_SURFACE_TYPE, &surfaceType);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_RED_SIZE, &r);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_GREEN_SIZE, &g);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_BLUE_SIZE, &b);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_ALPHA_SIZE, &a);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_DEPTH_SIZE, &depth);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_STENCIL_SIZE, &stencil);
            eglGetConfigAttrib(display_, configs[static_cast<size_t>(i)], EGL_SAMPLES, &samples);
            if ((renderable & EGL_OPENGL_ES3_BIT_KHR) == 0) continue;
            if ((surfaceType & EGL_PBUFFER_BIT) == 0) continue;
            if (r != 8 || g != 8 || b != 8 || a != 8) continue;
            if (pass == 0 && (depth != 0 || stencil != 0 || samples != 0)) continue;
            config_ = configs[static_cast<size_t>(i)];
            break;
        }
    }
    if (config_ == nullptr) {
        LOGE("No suitable EGL config among %d", static_cast<int>(numConfigs));
        return false;
    }

    const EGLint contextAttribs[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    context_ = eglCreateContext(display_, config_, EGL_NO_CONTEXT, contextAttribs);
    if (context_ == EGL_NO_CONTEXT) {
        LOGE("eglCreateContext (ES3) failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    const EGLint surfaceAttribs[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
    tinySurface_ = eglCreatePbufferSurface(display_, config_, surfaceAttribs);
    if (tinySurface_ == EGL_NO_SURFACE) {
        LOGE("eglCreatePbufferSurface failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    if (!eglMakeCurrent(display_, tinySurface_, tinySurface_, context_)) {
        LOGE("eglMakeCurrent failed: 0x%x", static_cast<unsigned>(eglGetError()));
        return false;
    }
    LOGI("EGL ES3 context current");
    return true;
}

void App::ShutdownEgl() {
    if (display_ == EGL_NO_DISPLAY) return;
    eglMakeCurrent(display_, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    if (tinySurface_ != EGL_NO_SURFACE) eglDestroySurface(display_, tinySurface_);
    if (context_ != EGL_NO_CONTEXT) eglDestroyContext(display_, context_);
    tinySurface_ = EGL_NO_SURFACE;
    context_ = EGL_NO_CONTEXT;
}

bool App::InitGl(JNIEnv* env) {
    // Surface swapchains for the compositor layers (ExoPlayer and the panel draw into these).
    videoChain_ = vrapi_CreateAndroidSurfaceSwapChain(kVideoSwapChainSize, kVideoSwapChainSize);
    panelChain_ = vrapi_CreateAndroidSurfaceSwapChain(kPanelWidth, kPanelHeight);
    if (videoChain_ == nullptr || panelChain_ == nullptr) {
        LOGE("vrapi_CreateAndroidSurfaceSwapChain failed (video %p, panel %p)",
             static_cast<void*>(videoChain_), static_cast<void*>(panelChain_));
        return false;
    }
    jobject video = vrapi_GetTextureSwapChainAndroidSurface(videoChain_);
    jobject panel = vrapi_GetTextureSwapChainAndroidSurface(panelChain_);
    if (video == nullptr || panel == nullptr) {
        LOGE("vrapi_GetTextureSwapChainAndroidSurface returned null");
        return false;
    }
    videoSurface_ = env->NewGlobalRef(video);
    panelSurface_ = env->NewGlobalRef(panel);
    if (videoSurface_ == nullptr || panelSurface_ == nullptr) {
        LOGE("NewGlobalRef failed for swapchain surfaces");
        return false;
    }

    // Sphere fallback path. Failure here only disables that mode.
    const bool essl3 = HasGlExtension("GL_OES_EGL_image_external_essl3");
    if (!external_.Create(env)) {
        LOGE("External texture setup failed: sphere fallback disabled");
    } else {
        const int w = vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_TEXTURE_WIDTH);
        const int h = vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_TEXTURE_HEIGHT);
        if (!sphere_.Init(w, h, essl3)) LOGE("Sphere renderer init failed: sphere fallback disabled");
    }
    return true;
}

void App::ShutdownGl(JNIEnv* env) {
    sphere_.Shutdown();
    external_.Destroy(env);
    if (videoSurface_ != nullptr) env->DeleteGlobalRef(videoSurface_);
    if (panelSurface_ != nullptr) env->DeleteGlobalRef(panelSurface_);
    videoSurface_ = nullptr;
    panelSurface_ = nullptr;
    if (videoChain_ != nullptr) vrapi_DestroyTextureSwapChain(videoChain_);
    if (panelChain_ != nullptr) vrapi_DestroyTextureSwapChain(panelChain_);
    videoChain_ = nullptr;
    panelChain_ = nullptr;
}

void App::LogDiagnostics() {
    LOGI("VrApi version: %s", vrapi_GetVersionString());
    LOGI("Device type: %d", vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_DEVICE_TYPE));
    LOGI("Display: %d x %d px, %.1f Hz",
         vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_DISPLAY_PIXELS_WIDE),
         vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_DISPLAY_PIXELS_HIGH),
         static_cast<double>(vrapi_GetSystemPropertyFloat(&java_, VRAPI_SYS_PROP_DISPLAY_REFRESH_RATE)));

    const int rateCount =
        vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_NUM_SUPPORTED_DISPLAY_REFRESH_RATES);
    std::string rates;
    if (rateCount > 0) {
        std::vector<float> values(static_cast<size_t>(rateCount), 0.0f);
        const int written = vrapi_GetSystemPropertyFloatArray(
            &java_, VRAPI_SYS_PROP_SUPPORTED_DISPLAY_REFRESH_RATES, values.data(), rateCount);
        for (int i = 0; i < written && i < rateCount; i++) {
            char buf[32];
            snprintf(buf, sizeof(buf), "%s%.1f", i == 0 ? "" : ", ",
                     static_cast<double>(values[static_cast<size_t>(i)]));
            rates += buf;
        }
    }
    LOGI("Supported refresh rates (%d): %s", rateCount, rates.c_str());

    LOGI("Suggested eye texture: %d x %d, FOV %.1f x %.1f deg",
         vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_TEXTURE_WIDTH),
         vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_TEXTURE_HEIGHT),
         static_cast<double>(vrapi_GetSystemPropertyFloat(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_FOV_DEGREES_X)),
         static_cast<double>(vrapi_GetSystemPropertyFloat(&java_, VRAPI_SYS_PROP_SUGGESTED_EYE_FOV_DEGREES_Y)));
    LOGI("Video decoder limit: %d", vrapi_GetSystemPropertyInt(&java_, VRAPI_SYS_PROP_VIDEO_DECODER_LIMIT));

    const char* renderer = reinterpret_cast<const char*>(glGetString(GL_RENDERER));
    const char* version = reinterpret_cast<const char*>(glGetString(GL_VERSION));
    LOGI("GL_RENDERER: %s", renderer ? renderer : "?");
    LOGI("GL_VERSION: %s", version ? version : "?");
    LOGI("GL_OES_EGL_image_external_essl3: %s",
         HasGlExtension("GL_OES_EGL_image_external_essl3") ? "yes" : "NO");
    LOGI("GL_OES_EGL_image_external: %s", HasGlExtension("GL_OES_EGL_image_external") ? "yes" : "NO");
}

void App::NotifySurfacesReady(JNIEnv* env) {
    jclass cls = env->GetObjectClass(activity_);
    jmethodID method = cls == nullptr ? nullptr
                                      : env->GetMethodID(cls, "onNativeSurfacesReady",
                                                         "(Landroid/view/Surface;Landroid/view/Surface;"
                                                         "Landroid/view/Surface;)V");
    if (cls != nullptr) env->DeleteLocalRef(cls);
    if (method == nullptr || JniFailed(env, "lookup onNativeSurfacesReady")) {
        LOGE("Activity.onNativeSurfacesReady not found");
        return;
    }
    env->CallVoidMethod(activity_, method, videoSurface_, panelSurface_, external_.surface());
    if (!JniFailed(env, "onNativeSurfacesReady")) LOGI("Handed swapchain surfaces to Java");
}

bool App::EnterVr(ANativeWindow* window) {
    ovrModeParms modeParms = vrapi_DefaultModeParms(&java_);
    modeParms.Flags |= VRAPI_MODE_FLAG_RESET_WINDOW_FULLSCREEN;
    modeParms.Flags |= VRAPI_MODE_FLAG_NATIVE_WINDOW;
    modeParms.Display = reinterpret_cast<size_t>(display_);
    modeParms.WindowSurface = reinterpret_cast<size_t>(window);
    modeParms.ShareContext = reinterpret_cast<size_t>(context_);

    ovr_ = vrapi_EnterVrMode(&modeParms);
    if (ovr_ == nullptr) {
        LOGE("vrapi_EnterVrMode failed (window %p)", static_cast<void*>(window));
        ANativeWindow_release(window);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            inVr_ = false;
        }
        cv_.notify_all();
        SetStatus("vrapi_EnterVrMode failed");
        return false;
    }
    activeWindow_ = window;
    LOGI("Entered VR mode");

    const uint32_t tid = CurrentThreadId();
    ovrResult r = vrapi_SetClockLevels(ovr_, kCpuLevel, kGpuLevel);
    if (r != ovrSuccess) LOGW("vrapi_SetClockLevels returned %d", static_cast<int>(r));
    r = vrapi_SetPerfThread(ovr_, VRAPI_PERF_THREAD_TYPE_MAIN, tid);
    if (r != ovrSuccess) LOGW("vrapi_SetPerfThread(MAIN) returned %d", static_cast<int>(r));
    r = vrapi_SetPerfThread(ovr_, VRAPI_PERF_THREAD_TYPE_RENDERER, tid);
    if (r != ovrSuccess) LOGW("vrapi_SetPerfThread(RENDERER) returned %d", static_cast<int>(r));

    lastMode_ = -1;
    fpsWindowStart_ = vrapi_GetTimeInSeconds();
    fpsFrames_ = 0;
    SetStatus("VR mode on");
    return true;
}

void App::LeaveVr() {
    if (ovr_ != nullptr) {
        vrapi_LeaveVrMode(ovr_);
        ovr_ = nullptr;
        LOGI("Left VR mode");
    }
    if (activeWindow_ != nullptr) {
        ANativeWindow_release(activeWindow_);
        activeWindow_ = nullptr;
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        inVr_ = false;
    }
    cv_.notify_all();
    SetStatus("VR mode off");
}

void App::UpdateFps(int mode) {
    fpsFrames_++;
    const double now = vrapi_GetTimeInSeconds();
    const double elapsed = now - fpsWindowStart_;
    if (elapsed < kFpsLogIntervalSeconds) return;
    const double fps = static_cast<double>(fpsFrames_) / elapsed;
    LOGI("Achieved %.1f fps over %.1f s (mode %d %s)", fps, elapsed, mode, ModeName(mode));
    char buf[96];
    snprintf(buf, sizeof(buf), "VR on, %.1f fps", fps);
    SetStatus(buf);
    fpsWindowStart_ = now;
    fpsFrames_ = 0;
}

void App::RunFrame(JNIEnv* env) {
    frameIndex_++;
    const double displayTime = vrapi_GetPredictedDisplayTime(ovr_, frameIndex_);
    const ovrTracking2 tracking = vrapi_GetPredictedTracking2(ovr_, displayTime);

    const int mode = mode_.load();
    if (mode != lastMode_) {
        LOGI("Render thread mode -> %d %s", mode, ModeName(mode));
        lastMode_ = mode;
    }

    ovrLayerProjection2 projectionLayer;
    ovrLayerEquirect2 equirectLayer;
    ovrLayerCylinder2 videoLayer;
    ovrLayerCylinder2 panelLayer;
    const ovrLayerHeader2* layers[3];
    uint32_t layerCount = 0;

    // Submit order: projection layer (sphere mode) -> video layer -> panel on top.
    switch (mode) {
        case kModeSphereFallback: {
            if (sphere_.ready() && external_.valid()) {
                if (!external_.Update(env, texMatrix_)) {
                    LOGW("SurfaceTexture update failed; reusing the previous frame");
                }
                const int index = static_cast<int>(frameIndex_ % sphere_.chainLength());
                sphere_.Render(tracking, index, external_.texture(), texMatrix_);
                ovrTextureSwapChain* eyeChains[2] = {sphere_.chain(0), sphere_.chain(1)};
                projectionLayer = MakeProjectionLayer(tracking, eyeChains, index);
                layers[layerCount++] = &projectionLayer.Header;
            } else if (!warnedSphere_) {
                LOGW("Sphere fallback selected but not available; showing the panel only");
                warnedSphere_ = true;
            }
            break;
        }
        case kModeCylinderFlat:
            videoLayer = MakeCylinderLayer(
                tracking, videoChain_, VideoScreenPlacement(videoAspect_.load()), false);
            layers[layerCount++] = &videoLayer.Header;
            break;
        case kModeEquirectStereoTb:
            equirectLayer = MakeEquirectLayer(tracking, videoChain_, true);
            layers[layerCount++] = &equirectLayer.Header;
            break;
        case kModeEquirectMono:
        default:
            equirectLayer = MakeEquirectLayer(tracking, videoChain_, false);
            layers[layerCount++] = &equirectLayer.Header;
            break;
    }

    panelLayer = MakeCylinderLayer(tracking, panelChain_, PanelPlacement(), true);
    layers[layerCount++] = &panelLayer.Header;

    ovrSubmitFrameDescription2 frameDesc = {};
    frameDesc.Flags = 0;
    frameDesc.SwapInterval = 1;
    frameDesc.FrameIndex = static_cast<uint64_t>(frameIndex_);
    frameDesc.DisplayTime = displayTime;
    frameDesc.LayerCount = layerCount;
    frameDesc.Layers = layers;

    const ovrResult result = vrapi_SubmitFrame2(ovr_, &frameDesc);
    if (result != lastSubmitResult_) {
        if (result < 0) LOGE("vrapi_SubmitFrame2 returned %d", static_cast<int>(result));
        else LOGI("vrapi_SubmitFrame2 returned %d", static_cast<int>(result));
        lastSubmitResult_ = result;
    }
    UpdateFps(mode);
}

}  // namespace syncvr
