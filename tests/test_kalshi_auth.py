from kalshi_auth import _normalize_pem


def test_normalize_pem_strips_accidental_angle_wrapper():
    pem = _normalize_pem("<QUJD>")

    assert pem.startswith("-----BEGIN")
    assert "\n<" not in pem
    assert "QUJD" in pem
