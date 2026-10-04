// Display modes. The numbering must match com.syncvr.player.core.DisplayMode (ordinal).
#pragma once

namespace syncvr {

enum Mode : int {
    kModeEquirectMono = 0,    // compositor equirect layer, same texture to both eyes
    kModeSphereFallback = 1,  // app-rendered sphere sampling a SurfaceTexture (projection layer)
    kModeCylinderFlat = 2,    // compositor cylinder layer, flat screen in front of the user
    kModeEquirectStereoTb = 3,  // compositor equirect layer, left eye = top half, right = bottom
    kModeCount = 4,
};

// Frame packing of the video texture. Must match com.syncvr.player.core.ViewStereo.code.
enum Stereo : int {
    kStereoMono = 0,
    kStereoTopBottom = 1,
    kStereoSideBySide = 2,
    kStereoCount = 3,
};

inline const char* ModeName(int mode) {
    switch (mode) {
        case kModeEquirectMono: return "EQUIRECT_MONO";
        case kModeSphereFallback: return "SPHERE_FALLBACK";
        case kModeCylinderFlat: return "CYLINDER_FLAT";
        case kModeEquirectStereoTb: return "EQUIRECT_STEREO_TB";
        default: return "UNKNOWN";
    }
}

}  // namespace syncvr
