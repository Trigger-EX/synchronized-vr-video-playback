#include "layers.h"

#include <math.h>

namespace syncvr {

namespace {

const float kPi = 3.14159265358979323846f;

// The compositor's cylinder mapping is fixed at 180 degrees around and 60 degrees vertical
// (VrApi_Types.h, ovrLayerCylinder2). Texture coordinate (u, v) in [0,1]^2 corresponds to
// azimuth (u - 0.5) * pi and to (y / horizontal distance) = (v - 0.5) * 2 * tan(30 degrees).
const float kCylinderHalfVerticalTan = 0.57735026919f;  // tan(30 degrees)

// Texture matrix layout from the header:  sx 0 tx 0 / 0 sy ty 0 / 0 0 1 0 / 0 0 0 1
void SetTextureMatrix(ovrMatrix4f* m, float sx, float sy, float tx, float ty) {
    *m = ovrMatrix4f_CreateIdentity();
    m->M[0][0] = sx;
    m->M[0][2] = tx;
    m->M[1][1] = sy;
    m->M[1][2] = ty;
}

// The layer's TexCoordsFromTanAngles for content fixed in the world around the viewer:
// the inverse of the view rotation (translation is cleared), exactly what the SDK uses for cube
// maps. The compositor re-projects with the latest head pose, so no further correction is needed.
ovrMatrix4f WorldFixedTexCoords(const ovrMatrix4f& viewMatrix) {
    return ovrMatrix4f_TanAngleMatrixForCubeMap(&viewMatrix);
}

}  // namespace

ScreenPlacement VideoScreenPlacement(float aspect) {
    if (!(aspect > 0.1f) || !(aspect < 10.0f)) aspect = 16.0f / 9.0f;
    const float kMaxWidth = 2.4f;   // 16:9 -> 2.4 x 1.35 m at 3 m: about 45 degrees wide
    const float kMaxHeight = 1.5f;  // keeps the top edge inside the +-30 degree band
    float height = kMaxWidth / aspect;
    if (height > kMaxHeight) height = kMaxHeight;
    const float bottomM = -0.3f;
    ScreenPlacement p;
    p.radiusM = 3.0f;
    p.widthM = height * aspect;
    p.heightM = height;
    p.centerYM = bottomM + 0.5f * height;
    return p;
}

ScreenPlacement PanelPlacement(bool prominent) {
    ScreenPlacement p;
    if (prominent) {
        p.radiusM = 3.0f;
        p.widthM = 2.4f;
        p.heightM = 0.6f;
        p.centerYM = 0.1f;
        return p;
    }
    p.radiusM = 3.0f;
    p.widthM = 1.2f;
    p.heightM = 0.3f;
    p.centerYM = -0.6f;
    return p;
}

float HeadYaw(const ovrTracking2& tracking) {
    const ovrQuatf& q = tracking.HeadPose.Pose.Orientation;
    // Forward is -Z rotated by q; yaw is the rotation about +Y that maps -Z onto it.
    return atan2f(2.0f * (q.x * q.z + q.w * q.y), 1.0f - 2.0f * (q.x * q.x + q.y * q.y));
}

ovrTracking2 RecenteredTracking(const ovrTracking2& tracking, float yaw) {
    // HW CHECK: sign of the yaw. Content direction d' = R(-yaw) * d, so the view becomes V * R(yaw).
    ovrTracking2 out = tracking;
    const ovrMatrix4f rot = ovrMatrix4f_CreateRotation(0.0f, yaw, 0.0f);
    for (int eye = 0; eye < VRAPI_FRAME_LAYER_EYE_MAX; eye++) {
        out.Eye[eye].ViewMatrix = ovrMatrix4f_Multiply(&tracking.Eye[eye].ViewMatrix, &rot);
    }
    return out;
}

ovrLayerEquirect2 MakeEquirectLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* chain, bool stereoTopBottom, float yaw) {
    ovrLayerEquirect2 layer = vrapi_DefaultLayerEquirect2();
    layer.HeadPose = tracking.HeadPose;
    const ovrMatrix4f plainView = vrapi_GetViewMatrixFromPose(&tracking.HeadPose.Pose);
    const ovrMatrix4f rot = ovrMatrix4f_CreateRotation(0.0f, yaw, 0.0f);
    const ovrMatrix4f headView = ovrMatrix4f_Multiply(&plainView, &rot);
    layer.TexCoordsFromTanAngles = WorldFixedTexCoords(headView);
    for (int eye = 0; eye < VRAPI_FRAME_LAYER_EYE_MAX; eye++) {
        layer.Textures[eye].ColorSwapChain = chain;
        layer.Textures[eye].SwapChainIndex = 0;
        if (stereoTopBottom) {
            // HW CHECK: assumes texture v = 1 is the top row of the video (OpenGL convention,
            // as delivered by a SurfaceTexture), so "top half" is v in [0.5, 1]. Left eye = top,
            // right eye = bottom. If the halves come out swapped or upside down, flip here.
            if (eye == VRAPI_FRAME_LAYER_EYE_LEFT) {
                layer.Textures[eye].TextureRect = {0.0f, 0.5f, 1.0f, 0.5f};
                SetTextureMatrix(&layer.Textures[eye].TextureMatrix, 1.0f, 0.5f, 0.0f, 0.5f);
            } else {
                layer.Textures[eye].TextureRect = {0.0f, 0.0f, 1.0f, 0.5f};
                SetTextureMatrix(&layer.Textures[eye].TextureMatrix, 1.0f, 0.5f, 0.0f, 0.0f);
            }
        }
    }
    layer.Header.Flags |= VRAPI_FRAME_LAYER_FLAG_CLIP_TO_TEXTURE_RECT;
    return layer;
}

ovrLayerCylinder2 MakeCylinderLayer(
    const ovrTracking2& tracking,
    ovrTextureSwapChain* chain,
    const ScreenPlacement& placement,
    bool opaque) {
    ovrLayerCylinder2 layer = vrapi_DefaultLayerCylinder2();
    layer.HeadPose = tracking.HeadPose;
    layer.Header.Flags |= VRAPI_FRAME_LAYER_FLAG_CLIP_TO_TEXTURE_RECT;
    if (opaque) {
        layer.Header.SrcBlend = VRAPI_FRAME_LAYER_BLEND_ONE;
        layer.Header.DstBlend = VRAPI_FRAME_LAYER_BLEND_ZERO;
    }

    // HW CHECK: the header only says the cylinder is "as if CUBE" with a fixed 180 x 60 degree
    // direction-to-hemicylinder mapping. We therefore feed it the world-fixed view rotation (same
    // as for cube maps) and select the wanted rectangle with TextureMatrix, centred straight
    // ahead (-Z of the tracking space). Distance is not a parameter of this mapping, so the
    // nominal 3 m only determines the angular size. Vertical v is assumed to grow upward.
    const float halfArc = 0.5f * placement.widthM / placement.radiusM;  // radians
    const float du = 2.0f * halfArc / kPi;
    const float u0 = 0.5f - halfArc / kPi;
    const float vScale = 2.0f * kCylinderHalfVerticalTan;
    const float dv = (placement.heightM / placement.radiusM) / vScale;
    const float v0 = 0.5f + ((placement.centerYM - 0.5f * placement.heightM) / placement.radiusM) / vScale;
    const float sx = 1.0f / du;
    const float sy = 1.0f / dv;

    for (int eye = 0; eye < VRAPI_FRAME_LAYER_EYE_MAX; eye++) {
        layer.Textures[eye].ColorSwapChain = chain;
        layer.Textures[eye].SwapChainIndex = 0;
        layer.Textures[eye].TexCoordsFromTanAngles = WorldFixedTexCoords(tracking.Eye[eye].ViewMatrix);
        SetTextureMatrix(&layer.Textures[eye].TextureMatrix, sx, sy, -u0 * sx, -v0 * sy);
    }
    return layer;
}

ovrLayerProjection2 MakeProjectionLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* const eyeChains[2], int swapChainIndex) {
    ovrLayerProjection2 layer = vrapi_DefaultLayerProjection2();
    layer.HeadPose = tracking.HeadPose;
    for (int eye = 0; eye < VRAPI_FRAME_LAYER_EYE_MAX; eye++) {
        layer.Textures[eye].ColorSwapChain = eyeChains[eye];
        layer.Textures[eye].SwapChainIndex = swapChainIndex;
        layer.Textures[eye].TexCoordsFromTanAngles =
            ovrMatrix4f_TanAngleMatrixFromProjection(&tracking.Eye[eye].ProjectionMatrix);
    }
    return layer;
}

}  // namespace syncvr
