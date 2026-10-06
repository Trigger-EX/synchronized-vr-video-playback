"""QOpenGLWidget showing the left eye of a 360/180/flat video with mouse look.

Import only where PySide6.QtOpenGLWidgets exists (guard with try/except ImportError).
"""
from __future__ import annotations

import array
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QMatrix3x3, QOpenGLFunctions
from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from . import projection as proj

_VERT = """
attribute vec2 pos;
varying vec2 ndc;
void main() { ndc = pos; gl_Position = vec4(pos, 0.0, 1.0); }
"""

_FRAG = """
#ifdef GL_ES
precision highp float;
#endif
varying vec2 ndc;
uniform sampler2D tex;
uniform mat3 view;
uniform float tanHalf;
uniform float aspect;
uniform vec4 eye;       // u0, v0, u1, v1 (top-left origin)
uniform int mode;       // 0 = 360, 1 = 180, 2 = flat
uniform vec4 flatRect;  // x, y, w, h in ndc-space 0..1 (flat letterbox)
const float PI = 3.14159265358979;
void main() {
    vec2 uv;
    if (mode == 2) {
        vec2 p = (ndc * 0.5 + 0.5 - flatRect.xy) / flatRect.zw;
        if (p.x < 0.0 || p.x > 1.0 || p.y < 0.0 || p.y > 1.0) { gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
        uv = vec2(p.x, 1.0 - p.y);
    } else {
        vec3 d = normalize(view * vec3(ndc.x * tanHalf * aspect, ndc.y * tanHalf, 1.0));
        float lon = atan(d.x, d.z);
        float lat = asin(clamp(d.y, -1.0, 1.0));
        if (mode == 1) {
            if (abs(lon) > PI * 0.5) { gl_FragColor = vec4(0.0, 0.0, 0.0, 1.0); return; }
            uv.x = lon / PI + 0.5;
        } else {
            uv.x = lon / (2.0 * PI) + 0.5;
        }
        uv.y = 0.5 - lat / PI;
    }
    vec2 t = mix(eye.xy, eye.zw, uv);
    gl_FragColor = vec4(texture2D(tex, t).rgb, 1.0);
}
"""


class SphereView(QOpenGLWidget):
    poseChanged = Signal(float, float, float)  # yaw, pitch, roll (user drag)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._dirty = False
        self._tex: QOpenGLTexture | None = None
        self._prog: QOpenGLShaderProgram | None = None
        self._projection, self._stereo, self._rotation = "360", "mono", 0.0
        self._yaw = self._pitch = self._roll = 0.0
        self._fov = proj.FOV_DEFAULT
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    # -- public API
    def set_frame(self, image: QImage) -> None:
        self._image = image
        if self.isVisible():  # skip texture upload (and repaint) while hidden
            self._dirty = True
            self.update()

    def set_view(self, projection: str, stereo: str, rotation: float = 0.0) -> None:
        self._projection = proj.norm_projection(projection)
        self._stereo = proj.norm_stereo(stereo)
        self._rotation = float(rotation or 0.0)
        self.update()

    def set_pose(self, yaw: float, pitch: float, roll: float = 0.0) -> None:
        self._yaw, self._pitch, self._roll = float(yaw), max(-90.0, min(90.0, float(pitch))), float(roll)
        self.update()

    def set_fov(self, fov: float) -> None:
        self._fov = proj.clamp_fov(fov)
        self.update()

    def fov(self) -> float:
        return self._fov

    def showEvent(self, e):
        if self._image is not None:
            self._dirty = True
        super().showEvent(e)

    # -- input
    def mousePressEvent(self, e):
        self._drag = e.position()
        self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, e):
        self._drag = None
        self.setCursor(Qt.OpenHandCursor)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        d = e.position() - self._drag
        self._drag = e.position()
        k = self._fov / max(self.height(), 1)  # degrees per pixel
        self.set_pose(self._yaw - d.x() * k, self._pitch + d.y() * k, self._roll)
        self.poseChanged.emit(self._yaw, self._pitch, self._roll)

    def wheelEvent(self, e):
        self.set_fov(self._fov - e.angleDelta().y() / 120.0 * 5.0)

    # -- GL
    def initializeGL(self):
        self._gl = QOpenGLFunctions(self.context())
        self._gl.initializeOpenGLFunctions()
        p = QOpenGLShaderProgram(self)
        p.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERT)
        p.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAG)
        p.bindAttributeLocation("pos", 0)
        p.link()
        self._prog = p

    def _upload(self):
        self._dirty = False
        if self._image is None or self._image.isNull():
            return
        if self._tex is not None:
            self._tex.destroy()
        self._tex = QOpenGLTexture(self._image.convertToFormat(QImage.Format_RGBA8888),
                                   QOpenGLTexture.DontGenerateMipMaps)
        self._tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
        self._tex.setWrapMode(QOpenGLTexture.ClampToEdge)

    def paintGL(self):
        gl = self._gl
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(0x4000)
        if not self.isVisible() or self._prog is None:
            return
        if self._dirty:
            self._upload()
        if self._tex is None or self._image is None:
            return
        w, h = max(self.width(), 1), max(self.height(), 1)
        m = proj.view_matrix(self._yaw, self._pitch, self._roll, self._rotation)
        r = proj.left_eye_rect(self._stereo)
        mode = {"360": 0, "180": 1, "flat": 2}[self._projection]
        ea = proj.eye_aspect(self._image.width(), self._image.height(), self._stereo)
        fx, fy, fw, fh = proj.flat_rect(ea, w, h)
        p = self._prog
        p.bind()
        self._tex.bind()
        p.setUniformValue1i(p.uniformLocation("tex"), 0)
        p.setUniformValue1i(p.uniformLocation("mode"), mode)
        p.setUniformValue1f(p.uniformLocation("tanHalf"), math.tan(math.radians(self._fov) / 2))
        p.setUniformValue1f(p.uniformLocation("aspect"), w / h)
        p.setUniformValue4f(p.uniformLocation("eye"), *r)
        p.setUniformValue4f(p.uniformLocation("flatRect"), fx / w, 1 - (fy + fh) / h, fw / w, fh / h)
        loc = p.uniformLocation("view")
        p.setUniformValue(loc, QMatrix3x3([c for row in m for c in row]))
        verts = array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1])
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(0, 2, 0x1406, 0, 0, verts.tobytes())
        gl.glDrawArrays(0x0005, 0, 4)
        p.release()
