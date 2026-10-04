// Builders for the compositor layers submitted every frame.
//
// Assumptions still needing headset confirmation are marked "UNVERIFIED:" in layers.cpp.
#pragma once

#include "VrApi.h"
#include "VrApi_Helpers.h"
#include "VrApi_Types.h"

namespace syncvr {

// A rectangle on a cylinder around the viewer. Sizes are in meters measured on the cylinder
// surface, the vertical centre is relative to eye height.
struct ScreenPlacement {
    float radiusM;
    float widthM;
    float heightM;
    float centerYM;
};

// Flat video screen about 3 m in front of the user, shaped to the video aspect ratio.
ScreenPlacement VideoScreenPlacement(float aspect);

// Small status panel below the video screen (1.2 m wide, 4:1); larger and centred for messages.
ScreenPlacement PanelPlacement(bool prominent);

// Yaw (radians, about +Y) of the direction the head is facing; 0 when facing -Z.
float HeadYaw(const ovrTracking2& tracking);

// Copy of `tracking` whose eye view matrices are rotated so that content which was fixed in the
// world is instead fixed relative to the direction `yaw` (recenter).
ovrTracking2 RecenteredTracking(const ovrTracking2& tracking, float yaw);

// Equirect layer. `stereo` is a Stereo from modes.h: mono shows the whole texture to both eyes;
// top/bottom gives the left eye the top half and the right eye the bottom half; side-by-side the
// left eye the left half and the right eye the right half. With `half180` the texture holds only
// the front 180 degrees (half-sphere) instead of the full 360.
ovrLayerEquirect2 MakeEquirectLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* chain, int stereo, bool half180, float yaw = 0.0f);

// Texture shown on a rectangle of the compositor's hemi-cylinder (see ScreenPlacement).
// `opaque` forces ONE/ZERO blending so the layer never shows what is behind it; otherwise the
// layer is blended premultiplied (ONE/ONE_MINUS_SRC_ALPHA). `chromaticAberrationCorrection`
// asks the compositor to correct colour fringes on this layer (small text/borders).
ovrLayerCylinder2 MakeCylinderLayer(
    const ovrTracking2& tracking,
    ovrTextureSwapChain* chain,
    const ScreenPlacement& placement,
    bool opaque,
    bool chromaticAberrationCorrection = false,
    int stereo = 0);  // Stereo from modes.h, same eye split as the equirect layer

// App-rendered eye buffers (sphere fallback).
ovrLayerProjection2 MakeProjectionLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* const eyeChains[2], int swapChainIndex);

}  // namespace syncvr
