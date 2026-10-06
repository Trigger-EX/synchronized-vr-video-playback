import math

from syncvr.gui import projection as P


def close(a, b, eps=1e-6):
    return all(abs(x - y) < eps for x, y in zip(a, b))


def test_left_eye_rects():
    assert P.left_eye_rect("mono") == (0, 0, 1, 1)
    assert P.left_eye_rect("tb") == (0, 0, 1, 0.5)
    assert P.left_eye_rect("sbs") == (0, 0, 0.5, 1)
    assert P.left_eye_rect("bogus") == (0, 0, 1, 1)


def test_eye_aspect():
    assert P.eye_aspect(3840, 1920, "mono") == 2.0
    assert P.eye_aspect(1920, 1920, "tb") == 2.0
    assert P.eye_aspect(3840, 1080, "sbs") == 1920 / 1080


def test_identity_looks_forward_centre():
    m = P.view_matrix(0, 0, 0)
    d = P.apply(m, (0, 0, 1))
    assert close(d, (0, 0, 1))
    assert close(P.sample_uv(d, "360"), (0.5, 0.5))
    assert close(P.sample_uv(d, "180"), (0.5, 0.5))


def test_yaw_right_and_pitch_up():
    d = P.apply(P.view_matrix(90, 0, 0), (0, 0, 1))
    assert close(d, (1, 0, 0))
    assert close(P.sample_uv(d, "360"), (0.75, 0.5))
    assert close(P.sample_uv(d, "180"), (1.0, 0.5))
    d = P.apply(P.view_matrix(0, 90, 0), (0, 0, 1))
    assert close(d, (0, 1, 0))
    assert close(P.sample_uv(d, "360"), (0.5, 0.0))


def test_rotation_offset_turns_content():
    a = P.apply(P.view_matrix(0, 0, 0, rotation=90), (0, 0, 1))
    assert close(a, (-1, 0, 0))


def test_roll_keeps_forward():
    m = P.view_matrix(0, 0, 45)
    assert close(P.apply(m, (0, 0, 1)), (0, 0, 1))
    assert close(P.apply(m, (1, 0, 0)), (math.cos(math.pi / 4), math.sin(math.pi / 4), 0))


def test_180_outside_front_is_none():
    assert P.sample_uv((0, 0, -1), "180") is None
    assert close(P.sample_uv((0, 0, -1), "360"), (1.0, 0.5)) or close(P.sample_uv((0, 0, -1), "360"), (0.0, 0.5))


def test_ray_dir_centre_and_edge():
    assert close(P.ray_dir(0, 0, 90, 1.0), (0, 0, 1))
    x, y, z = P.ray_dir(0, 1, 90, 1.0)
    assert abs(math.degrees(math.atan2(y, z)) - 45) < 1e-6


def test_fov_clamp_and_normalizers():
    assert P.clamp_fov(5) == P.FOV_MIN and P.clamp_fov(500) == P.FOV_MAX
    assert P.norm_projection(None) == "360" and P.norm_projection(" FLAT ") == "flat"
    assert P.norm_stereo("SBS") == "sbs"


def test_flat_rect_letterbox():
    x, y, w, h = P.flat_rect(2.0, 1000, 1000)
    assert (x, w, h) == (0, 1000, 500) and y == 250
    x, y, w, h = P.flat_rect(2.0, 1000, 200)
    assert (y, h, w) == (0, 200, 400) and x == 300
