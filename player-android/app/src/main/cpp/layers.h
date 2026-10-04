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

// Full 360x180 equirect layer. With stereoTopBottom the left eye shows the top half of the
// texture and the right eye the bottom half, otherwise both eyes show the whole texture.
ovrLayerEquirect2 MakeEquirectLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* chain, bool stereoTopBottom, float yaw = 0.0f);

// Texture shown on a rectangle of the compositor's hemi-cylinder (see ScreenPlacement).
// `opaque` forces ONE/ZERO blending so the panel never shows what is behind it.
ovrLayerCylinder2 MakeCylinderLayer(
    const ovrTracking2& tracking,
    ovrTextureSwapChain* chain,
    const ScreenPlacement& placement,
    bool opaque);

// App-rendered eye buffers (sphere fallback).
ovrLayerProjection2 MakeProjectionLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* const eyeChains[2], int swapChainIndex);

}  // namespace syncvr
