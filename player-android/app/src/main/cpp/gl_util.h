// Small OpenGL ES helpers: error checks, shader compilation, extension query.
#pragma once

#include <GLES3/gl3.h>

namespace syncvr {

// Logs and returns true if a GL error is pending. `what` names the call site.
bool CheckGl(const char* what);

// Returns 0 on failure (the compiler log is written to logcat).
GLuint BuildProgram(const char* vertexSource, const char* fragmentSource, const char* name);

// True if `name` appears in the space separated GL_EXTENSIONS string.
bool HasGlExtension(const char* name);

}  // namespace syncvr
