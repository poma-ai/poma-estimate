from PIL import Image
from poma_estimate.estimators.image import estimate_image


def test_large_photo_is_page_like(tmp_path):
    p = tmp_path / "scan.jpg"
    Image.effect_noise((1600, 2200), 80).convert("RGB").save(p, quality=90)
    band, d = estimate_image(p)
    assert d["complexity"] == "page-like"
    assert band.expected >= 2


def test_small_image_stays_one_unit(tmp_path):
    p = tmp_path / "s.png"
    Image.new("RGB", (300, 300), "white").save(p)
    band, d = estimate_image(p)
    assert d["complexity"] != "page-like"
    assert band.expected == 1
