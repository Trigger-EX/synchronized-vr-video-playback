// Sphere fallback: draws the video (an external OES texture) on the inside of a sphere into
// per-eye texture swapchains that are then submitted as an ovrLayerProjection2.
// All methods must be called on the render thread with the GL context current.
#pragma once

#include <GLES3/gl3.h>

#include <vector>

#include "VrApi.h"
#include "VrApi_Types.h"

namespace syncvr {

class SphereRenderer {
public:
    SphereRenderer() = default;
    SphereRenderer(const SphereRenderer&) = delete;
    SphereRenderer& operator=(const SphereRenderer&) = delete;

    // `eyeWidth`/`eyeHeight` come from VRAPI_SYS_PROP_SUGGESTED_EYE_TEXTURE_*.
    bool Init(int eyeWidth, int eyeHeight, bool hasExternalEssl3);
    void Shutdown();

    bool ready() const { return ready_; }
    ovrTextureSwapChain* chain(int eye) const { return chains_[eye]; }
    int chainLength() const { return chainLength_; }

    // Renders both eyes into swapchain image `index`. `texMatrix` is the SurfaceTexture
    // transform (column-major).
    void Render(const ovrTracking2& tracking, int index, GLuint oesTexture, const float* texMatrix);

private:
    bool BuildMesh();

    bool ready_ = false;
    int width_ = 0;
    int height_ = 0;
    ovrTextureSwapChain* chains_[2] = {nullptr, nullptr};
    int chainLength_ = 0;
    std::vector<GLuint> framebuffers_[2];

    GLuint program_ = 0;
    GLint mvpLocation_ = -1;
    GLint texMatrixLocation_ = -1;
    GLint samplerLocation_ = -1;
    GLint posAttrib_ = -1;
    GLint uvAttrib_ = -1;
    GLuint vertexBuffer_ = 0;
    GLuint indexBuffer_ = 0;
    GLsizei indexCount_ = 0;
};

}  // namespace syncvr
