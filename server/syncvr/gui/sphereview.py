"""QOpenGLWidget showing the left eye of a 360/180/flat video with mouse look.

Import only where PySide6.QtOpenGLWidgets exists (guard with try/except ImportError).
"""
from __future__ import annotations

import array
import logging
import math

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QOpenGLFunctions, QPainter
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from . import projection as proj

log = logging.getLogger(__name__)
MAX_TEX_W = 2048  # frames (5120x2560 HEVC...) are downscaled on the CPU to this width before upload

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
uniform vec3 viewR0;
uniform vec3 viewR1;
uniform vec3 viewR2;
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
        vec3 v = vec3(ndc.x * tanHalf * aspect, ndc.y * tanHalf, 1.0);
        vec3 d = normalize(vec3(dot(viewR0, v), dot(viewR1, v), dot(viewR2, v)));
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
    problem = Signal(str)  # GL init/paint failure; the view keeps working through a CPU fallback

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image: QImage | None = None
        self._small: QImage | None = None  # downscaled copy used for both GL upload and CPU painting
        self._dirty = False
        self._tex: QOpenGLTexture | None = None
        self._prog: QOpenGLShaderProgram | None = None
        self._vao = None
        self._vbo = None
        self.failed: str | None = None
        self._status = ""
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

    def set_status(self, text: str) -> None:
        """Overlay message (errors, 'no frames'); empty clears it."""
        if text != self._status:
            self._status = text or ""
            self.update()

    def _fail(self, reason: str) -> None:
        if self.failed is None:
            self.failed = reason
            log.error("SphereView: GL rendering disabled, using CPU fallback: %s", reason)
            self._dirty = True
            self.problem.emit(reason)

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
        try:
            self._init_gl()
        except Exception as e:  # noqa: BLE001 - any GL problem must degrade, not blank the window
            log.exception("SphereView: GL init failed")
            self._fail("GL init failed: %s" % e)

    def _init_gl(self) -> None:
        ctx = self.context()
        fmt = ctx.format()
        log.info("SphereView: GL context %d.%d profile=%s", fmt.majorVersion(), fmt.minorVersion(),
                 fmt.profile().name if hasattr(fmt.profile(), "name") else fmt.profile())
        self._gl = QOpenGLFunctions(ctx)
        self._gl.initializeOpenGLFunctions()
        try:
            self._vao = QOpenGLVertexArrayObject(self)
            if self._vao.create():
                self._vao.bind()
            else:
                self._vao = None
        except Exception:  # noqa: BLE001
            log.warning("SphereView: no VAO support", exc_info=True)
            self._vao = None
        p = QOpenGLShaderProgram(self)
        if not p.addShaderFromSourceCode(QOpenGLShader.Vertex, _VERT):
            raise RuntimeError("vertex shader compile failed: %s" % p.log())
        if not p.addShaderFromSourceCode(QOpenGLShader.Fragment, _FRAG):
            raise RuntimeError("fragment shader compile failed: %s" % p.log())
        p.bindAttributeLocation("pos", 0)
        if not p.link():
            raise RuntimeError("shader link failed: %s" % p.log())
        if p.log():
            log.info("SphereView: shader log: %s", p.log())
        self._prog = p
        data = array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes()
        vbo = QOpenGLBuffer(QOpenGLBuffer.VertexBuffer)
        if not vbo.create():
            raise RuntimeError("vertex buffer create failed")
        vbo.bind()
        vbo.allocate(data, len(data))
        self._vbo = vbo
        if self._vao is not None:
            self._vao.release()

    def _prepare(self) -> None:
        """Downscaled copy of the newest frame (cheap to upload and to paint)."""
        self._dirty = False
        img = self._image
        if img is None or img.isNull():
            self._small = None
            return
        if img.width() > MAX_TEX_W:
            img = img.scaledToWidth(MAX_TEX_W, Qt.FastTransformation)
        self._small = img
        if self.failed is None:
            if self._tex is not None:
                self._tex.destroy()
                self._tex = None
            tex = QOpenGLTexture(img.convertToFormat(QImage.Format_RGBA8888), QOpenGLTexture.DontGenerateMipMaps)
            if not tex.isCreated():
                raise RuntimeError("texture creation failed for %dx%d image" % (img.width(), img.height()))
            tex.setMinMagFilters(QOpenGLTexture.Linear, QOpenGLTexture.Linear)
            tex.setWrapMode(QOpenGLTexture.ClampToEdge)
            self._tex = tex

    def paintGL(self):
        try:
            if self.failed is None and self._prog is not None:
                self._paint_gl()
                return
        except Exception as e:  # noqa: BLE001
            log.exception("SphereView: paintGL failed")
            self._fail("GL paint failed: %s" % e)
        self._paint_cpu()

    def _paint_gl(self) -> None:
        gl = self._gl
        gl.glClearColor(0, 0, 0, 1)
        gl.glClear(0x4000)
        if not self.isVisible():
            return
        if self._dirty:
            self._prepare()
        if self._tex is None or self._small is None:
            self._overlay(self._status or "Waiting for video\u2026")
            return
        w, h = max(self.width(), 1), max(self.height(), 1)
        m = proj.view_matrix(self._yaw, self._pitch, self._roll, self._rotation)
        r = proj.left_eye_rect(self._stereo)
        mode = {"360": 0, "180": 1, "flat": 2}[self._projection]
        ea = proj.eye_aspect(self._small.width(), self._small.height(), self._stereo)
        fx, fy, fw, fh = proj.flat_rect(ea, w, h)
        p = self._prog
        gl.glGetError()  # clear stale errors
        p.bind()
        if self._vao is not None:
            self._vao.bind()
        self._tex.bind()
        p.setUniformValue1i(p.uniformLocation("tex"), 0)
        p.setUniformValue1i(p.uniformLocation("mode"), mode)
        p.setUniformValue1f(p.uniformLocation("tanHalf"), math.tan(math.radians(self._fov) / 2))
        p.setUniformValue1f(p.uniformLocation("aspect"), w / h)
        p.setUniformValue4f(p.uniformLocation("eye"), *r)
        p.setUniformValue4f(p.uniformLocation("flatRect"), fx / w, 1 - (fy + fh) / h, fw / w, fh / h)
        for i in range(3):
            p.setUniformValue(p.uniformLocation("viewR%d" % i), float(m[i][0]), float(m[i][1]), float(m[i][2]))
        self._vbo.bind()
        p.enableAttributeArray(0)
        p.setAttributeBuffer(0, 0x1406, 0, 2, 8)
        gl.glDrawArrays(0x0005, 0, 4)
        p.disableAttributeArray(0)
        self._vbo.release()
        if self._vao is not None:
            self._vao.release()
        p.release()
        err = gl.glGetError()
        if err:
            raise RuntimeError("glGetError 0x%x after draw" % err)
        if self._status:
            self._overlay(self._status)

    # -- CPU fallback / overlay
    def _paint_cpu(self) -> None:
        if self._dirty:
            try:
                self._prepare()
            except Exception:  # noqa: BLE001
                log.exception("SphereView: frame preparation failed")
        pt = QPainter(self)
        try:
            pt.fillRect(self.rect(), QColor(0, 0, 0))
            img = self._small
            if img is not None and not img.isNull():
                u0, v0, u1, v1 = proj.left_eye_rect(self._stereo)
                src = QRectF(u0 * img.width(), v0 * img.height(), (u1 - u0) * img.width(), (v1 - v0) * img.height())
                k = min(self.width() / max(src.width(), 1), self.height() / max(src.height(), 1))
                dw, dh = src.width() * k, src.height() * k
                dst = QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)
                pt.drawImage(dst, img, src)
            self._draw_text(pt, self._status or (("GL unavailable (%s) - flat preview" % self.failed)
                                                 if self.failed and img is not None else
                                                 self.failed or "Waiting for video\u2026"))
        finally:
            pt.end()

    def _overlay(self, text: str) -> None:
        pt = QPainter(self)
        try:
            self._draw_text(pt, text)
        finally:
            pt.end()

    def _draw_text(self, pt: QPainter, text: str) -> None:
        if not text:
            return
        r = QRectF(8, 8, max(self.width() - 16, 1), max(self.height() - 16, 1))
        pt.setPen(QColor(255, 255, 255))
        box = pt.boundingRect(r, Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap, text)
        pt.fillRect(box.adjusted(-4, -2, 4, 2), QColor(0, 0, 0, 170))
        pt.drawText(r, Qt.AlignTop | Qt.AlignLeft | Qt.TextWordWrap, text)
