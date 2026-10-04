// Builders for the compositor layers submitted every frame.
//
// Assumptions that need a headset to confirm are marked "HW CHECK" in layers.cpp.
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

// Small status panel below the video screen (1.2 m wide, 4:1).
ScreenPlacement PanelPlacement();

// Full 360x180 equirect layer. With stereoTopBottom the left eye shows the top half of the
// texture and the right eye the bottom half, otherwise both eyes show the whole texture.
ovrLayerEquirect2 MakeEquirectLayer(
    const ovrTracking2& tracking, ovrTextureSwapChain* chain, bool stereoTopBottom);

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
