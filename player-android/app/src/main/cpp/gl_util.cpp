#include "gl_util.h"

#include <string.h>

#include <string>
#include <vector>

#include "log.h"

namespace syncvr {

bool CheckGl(const char* what) {
    bool any = false;
    for (int i = 0; i < 8; i++) {
        const GLenum err = glGetError();
        if (err == GL_NO_ERROR) break;
        LOGE("GL error 0x%x after %s", static_cast<unsigned>(err), what);
        any = true;
    }
    return any;
}

static GLuint CompileShader(GLenum type, const char* source, const char* name) {
    const GLuint shader = glCreateShader(type);
    if (shader == 0) {
        LOGE("glCreateShader failed for %s", name);
        return 0;
    }
    glShaderSource(shader, 1, &source, nullptr);
    glCompileShader(shader);
    GLint ok = 0;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (ok == GL_FALSE) {
        GLint len = 0;
        glGetShaderiv(shader, GL_INFO_LOG_LENGTH, &len);
        std::vector<char> log(static_cast<size_t>(len > 1 ? len : 1), 0);
        glGetShaderInfoLog(shader, static_cast<GLsizei>(log.size()), nullptr, log.data());
        LOGE("%s %s shader compile failed: %s", name,
             type == GL_VERTEX_SHADER ? "vertex" : "fragment", log.data());
        glDeleteShader(shader);
        return 0;
    }
    return shader;
}

GLuint BuildProgram(const char* vertexSource, const char* fragmentSource, const char* name) {
    const GLuint vs = CompileShader(GL_VERTEX_SHADER, vertexSource, name);
    if (vs == 0) return 0;
    const GLuint fs = CompileShader(GL_FRAGMENT_SHADER, fragmentSource, name);
    if (fs == 0) {
        glDeleteShader(vs);
        return 0;
    }
    const GLuint program = glCreateProgram();
    glAttachShader(program, vs);
    glAttachShader(program, fs);
    glLinkProgram(program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint ok = 0;
    glGetProgramiv(program, GL_LINK_STATUS, &ok);
    if (ok == GL_FALSE) {
        GLint len = 0;
        glGetProgramiv(program, GL_INFO_LOG_LENGTH, &len);
        std::vector<char> log(static_cast<size_t>(len > 1 ? len : 1), 0);
        glGetProgramInfoLog(program, static_cast<GLsizei>(log.size()), nullptr, log.data());
        LOGE("%s program link failed: %s", name, log.data());
        glDeleteProgram(program);
        return 0;
    }
    return program;
}

bool HasGlExtension(const char* name) {
    const char* all = reinterpret_cast<const char*>(glGetString(GL_EXTENSIONS));
    if (all == nullptr || name == nullptr) return false;
    const size_t len = strlen(name);
    const char* p = all;
    while ((p = strstr(p, name)) != nullptr) {
        const bool startOk = (p == all) || p[-1] == ' ';
        const bool endOk = p[len] == ' ' || p[len] == '\0';
        if (startOk && endOk) return true;
        p += len;
    }
    return false;
}

}  // namespace syncvr
