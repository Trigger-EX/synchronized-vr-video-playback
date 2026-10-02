#include "sphere_renderer.h"

#include <GLES2/gl2ext.h>
#include <math.h>

#include "VrApi_Helpers.h"
#include "gl_util.h"
#include "log.h"

namespace syncvr {

namespace {

const int kSphereLonSegments = 64;
const int kSphereLatSegments = 32;
const float kSphereRadius = 10.0f;
const float kPi = 3.14159265358979323846f;

const char* kVertexEssl3 =
    "#version 300 es\n"
    "uniform mat4 uMvp;\n"
    "uniform mat4 uTexMatrix;\n"
    "in vec3 aPos;\n"
    "in vec2 aUv;\n"
    "out vec2 vUv;\n"
    "void main() {\n"
    "  gl_Position = uMvp * vec4(aPos, 1.0);\n"
    "  vUv = (uTexMatrix * vec4(aUv, 0.0, 1.0)).xy;\n"
    "}\n";

const char* kFragmentEssl3 =
    "#version 300 es\n"
    "#extension GL_OES_EGL_image_external_essl3 : require\n"
    "precision mediump float;\n"
    "uniform samplerExternalOES uTexture;\n"
    "in vec2 vUv;\n"
    "out vec4 outColor;\n"
    "void main() {\n"
    "  outColor = texture(uTexture, vUv);\n"
    "}\n";

// Used when the driver lacks GL_OES_EGL_image_external_essl3.
const char* kVertexEssl1 =
    "uniform mat4 uMvp;\n"
    "uniform mat4 uTexMatrix;\n"
    "attribute vec3 aPos;\n"
    "attribute vec2 aUv;\n"
    "varying vec2 vUv;\n"
    "void main() {\n"
    "  gl_Position = uMvp * vec4(aPos, 1.0);\n"
    "  vUv = (uTexMatrix * vec4(aUv, 0.0, 1.0)).xy;\n"
    "}\n";

const char* kFragmentEssl1 =
    "#extension GL_OES_EGL_image_external : require\n"
    "precision mediump float;\n"
    "uniform samplerExternalOES uTexture;\n"
    "varying vec2 vUv;\n"
    "void main() {\n"
    "  gl_FragColor = texture2D(uTexture, vUv);\n"
    "}\n";

}  // namespace

bool SphereRenderer::BuildMesh() {
    // Vertex layout: x y z u v. Azimuth 0 is straight ahead (-Z), u = 0.5 + azimuth / (2 pi),
    // v = 0.5 + elevation / pi, matching the equirect convention used by the compositor layers.
    const int cols = kSphereLonSegments + 1;
    const int rows = kSphereLatSegments + 1;
    std::vector<float> vertices;
    vertices.reserve(static_cast<size_t>(cols) * rows * 5);
    for (int i = 0; i < rows; i++) {
        const float v = static_cast<float>(i) / kSphereLatSegments;
        const float elevation = kPi * (v - 0.5f);
        for (int j = 0; j < cols; j++) {
            const float u = static_cast<float>(j) / kSphereLonSegments;
            const float azimuth = 2.0f * kPi * (u - 0.5f);
            vertices.push_back(kSphereRadius * cosf(elevation) * sinf(azimuth));
            vertices.push_back(kSphereRadius * sinf(elevation));
            vertices.push_back(-kSphereRadius * cosf(elevation) * cosf(azimuth));
            vertices.push_back(u);
            vertices.push_back(v);
        }
    }
    std::vector<GLushort> indices;
    indices.reserve(static_cast<size_t>(kSphereLonSegments) * kSphereLatSegments * 6);
    for (int i = 0; i < kSphereLatSegments; i++) {
        for (int j = 0; j < kSphereLonSegments; j++) {
            const GLushort a = static_cast<GLushort>(i * cols + j);
            const GLushort b = static_cast<GLushort>(a + 1);
            const GLushort c = static_cast<GLushort>(a + cols);
            const GLushort d = static_cast<GLushort>(c + 1);
            indices.push_back(a);
            indices.push_back(c);
            indices.push_back(b);
            indices.push_back(b);
            indices.push_back(c);
            indices.push_back(d);
        }
    }
    indexCount_ = static_cast<GLsizei>(indices.size());

    glGenBuffers(1, &vertexBuffer_);
    glBindBuffer(GL_ARRAY_BUFFER, vertexBuffer_);
    glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(vertices.size() * sizeof(float)),
                 vertices.data(), GL_STATIC_DRAW);
    glGenBuffers(1, &indexBuffer_);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, indexBuffer_);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, static_cast<GLsizeiptr>(indices.size() * sizeof(GLushort)),
                 indices.data(), GL_STATIC_DRAW);
    glBindBuffer(GL_ARRAY_BUFFER, 0);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0);
    return !CheckGl("sphere mesh upload");
}

bool SphereRenderer::Init(int eyeWidth, int eyeHeight, bool hasExternalEssl3) {
    width_ = eyeWidth;
    height_ = eyeHeight;

    program_ = hasExternalEssl3 ? BuildProgram(kVertexEssl3, kFragmentEssl3, "sphere essl3")
                                : 0;
    if (program_ == 0) {
        if (hasExternalEssl3) LOGW("essl3 sphere shader failed, trying the ES 1.00 variant");
        program_ = BuildProgram(kVertexEssl1, kFragmentEssl1, "sphere essl1");
    }
    if (program_ == 0) {
        LOGE("Sphere fallback unavailable: no shader compiled");
        return false;
    }
    mvpLocation_ = glGetUniformLocation(program_, "uMvp");
    texMatrixLocation_ = glGetUniformLocation(program_, "uTexMatrix");
    samplerLocation_ = glGetUniformLocation(program_, "uTexture");
    posAttrib_ = glGetAttribLocation(program_, "aPos");
    uvAttrib_ = glGetAttribLocation(program_, "aUv");
    if (mvpLocation_ < 0 || texMatrixLocation_ < 0 || samplerLocation_ < 0 || posAttrib_ < 0 ||
        uvAttrib_ < 0) {
        LOGE("Sphere shader locations missing (mvp %d tex %d sampler %d pos %d uv %d)",
             mvpLocation_, texMatrixLocation_, samplerLocation_, posAttrib_, uvAttrib_);
        Shutdown();
        return false;
    }
    if (!BuildMesh()) {
        Shutdown();
        return false;
    }

    for (int eye = 0; eye < 2; eye++) {
        chains_[eye] = vrapi_CreateTextureSwapChain3(
            VRAPI_TEXTURE_TYPE_2D, GL_RGBA8, width_, height_, 1, 3);
        if (chains_[eye] == nullptr) {
            LOGE("vrapi_CreateTextureSwapChain3 failed for eye %d (%dx%d)", eye, width_, height_);
            Shutdown();
            return false;
        }
    }
    chainLength_ = vrapi_GetTextureSwapChainLength(chains_[0]);
    if (chainLength_ <= 0 || chainLength_ != vrapi_GetTextureSwapChainLength(chains_[1])) {
        LOGE("Unexpected eye swapchain length %d", chainLength_);
        Shutdown();
        return false;
    }

    for (int eye = 0; eye < 2; eye++) {
        framebuffers_[eye].assign(static_cast<size_t>(chainLength_), 0);
        for (int i = 0; i < chainLength_; i++) {
            const GLuint texture = vrapi_GetTextureSwapChainHandle(chains_[eye], i);
            glBindTexture(GL_TEXTURE_2D, texture);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            glBindTexture(GL_TEXTURE_2D, 0);

            GLuint fbo = 0;
            glGenFramebuffers(1, &fbo);
            framebuffers_[eye][static_cast<size_t>(i)] = fbo;
            glBindFramebuffer(GL_FRAMEBUFFER, fbo);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture, 0);
            const GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
            glBindFramebuffer(GL_FRAMEBUFFER, 0);
            if (status != GL_FRAMEBUFFER_COMPLETE) {
                LOGE("Eye framebuffer %d/%d incomplete: 0x%x", eye, i, static_cast<unsigned>(status));
                Shutdown();
                return false;
            }
        }
    }
    if (CheckGl("sphere eye framebuffers")) {
        Shutdown();
        return false;
    }
    ready_ = true;
    LOGI("Sphere renderer ready: eye buffers %dx%d, %d images per eye, %s shader", width_, height_,
         chainLength_, hasExternalEssl3 ? "essl3" : "essl1");
    return true;
}

void SphereRenderer::Shutdown() {
    ready_ = false;
    for (int eye = 0; eye < 2; eye++) {
        for (GLuint fbo : framebuffers_[eye]) {
            if (fbo != 0) glDeleteFramebuffers(1, &fbo);
        }
        framebuffers_[eye].clear();
        if (chains_[eye] != nullptr) {
            vrapi_DestroyTextureSwapChain(chains_[eye]);
            chains_[eye] = nullptr;
        }
    }
    chainLength_ = 0;
    if (vertexBuffer_ != 0) glDeleteBuffers(1, &vertexBuffer_);
    if (indexBuffer_ != 0) glDeleteBuffers(1, &indexBuffer_);
    vertexBuffer_ = 0;
    indexBuffer_ = 0;
    if (program_ != 0) glDeleteProgram(program_);
    program_ = 0;
}

void SphereRenderer::Render(
    const ovrTracking2& tracking, int index, GLuint oesTexture, const float* texMatrix) {
    if (!ready_ || index < 0 || index >= chainLength_) return;

    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_BLEND);
    glDisable(GL_SCISSOR_TEST);

    for (int eye = 0; eye < 2; eye++) {
        glBindFramebuffer(GL_FRAMEBUFFER, framebuffers_[eye][static_cast<size_t>(index)]);
        glViewport(0, 0, width_, height_);
        glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        glClear(GL_COLOR_BUFFER_BIT);

        // Rotation only: the sphere is infinitely far away, so no eye-separation parallax.
        ovrMatrix4f view = tracking.Eye[eye].ViewMatrix;
        view.M[0][3] = 0.0f;
        view.M[1][3] = 0.0f;
        view.M[2][3] = 0.0f;
        const ovrMatrix4f mvp = ovrMatrix4f_Multiply(&tracking.Eye[eye].ProjectionMatrix, &view);

        glUseProgram(program_);
        // ovrMatrix4f is row-major; transpose on upload (allowed in ES 3.0).
        glUniformMatrix4fv(mvpLocation_, 1, GL_TRUE, &mvp.M[0][0]);
        // SurfaceTexture's matrix is already column-major.
        glUniformMatrix4fv(texMatrixLocation_, 1, GL_FALSE, texMatrix);
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, oesTexture);
        glUniform1i(samplerLocation_, 0);

        glBindBuffer(GL_ARRAY_BUFFER, vertexBuffer_);
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, indexBuffer_);
        const GLsizei stride = 5 * sizeof(float);
        glEnableVertexAttribArray(static_cast<GLuint>(posAttrib_));
        glVertexAttribPointer(static_cast<GLuint>(posAttrib_), 3, GL_FLOAT, GL_FALSE, stride,
                              reinterpret_cast<const void*>(0));
        glEnableVertexAttribArray(static_cast<GLuint>(uvAttrib_));
        glVertexAttribPointer(static_cast<GLuint>(uvAttrib_), 2, GL_FLOAT, GL_FALSE, stride,
                              reinterpret_cast<const void*>(3 * sizeof(float)));
        glDrawElements(GL_TRIANGLES, indexCount_, GL_UNSIGNED_SHORT, nullptr);
        glDisableVertexAttribArray(static_cast<GLuint>(posAttrib_));
        glDisableVertexAttribArray(static_cast<GLuint>(uvAttrib_));
        glBindBuffer(GL_ARRAY_BUFFER, 0);
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0);
        glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);
    }
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glUseProgram(0);
    glFlush();
    CheckGl("sphere render");
}

}  // namespace syncvr
