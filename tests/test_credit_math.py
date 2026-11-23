"""Pin the credit math against the platform's credit rule."""

from poma_estimate.credit_math import CreditBand, band_for, credits_for


def test_pages_dominate():
    # 100 pages, 50k tokens → max(100, 50) = 100
    assert credits_for(pages=100, tokens=50_000) == 100


def test_tokens_dominate():
    # 1 page, 50k tokens → max(1, 50) = 50
    assert credits_for(pages=1, tokens=50_000) == 50


def test_ceiling_on_token_blocks():
    # 1001 tokens rounds up to 2 blocks
    assert credits_for(pages=0, tokens=1001) == 2
    assert credits_for(pages=0, tokens=1000) == 1
    assert credits_for(pages=0, tokens=1) == 1


def test_zero_inputs():
    assert credits_for(pages=0, tokens=0) == 0


def test_band_addition():
    a = CreditBand(1, 2, 3)
    b = CreditBand(10, 20, 30)
    assert (a + b) == CreditBand(11, 22, 33)


def test_band_collapses_when_tokens_fixed():
    band = band_for(pages=5, tokens_low=2000, tokens_expected=2000, tokens_high=2000)
    assert band.low == band.expected == band.high == 5  # pages dominate


def test_band_widens_when_tokens_uncertain():
    band = band_for(pages=1, tokens_low=500, tokens_expected=2500, tokens_high=6000)
    assert band.low == 1   # 1 page, 1 block
    assert band.expected == 3
    assert band.high == 6


def test_round_estimate_ladder():
    from poma_estimate.credit_math import round_estimate
    # ≤9: exact
    assert round_estimate(0) == 0
    assert round_estimate(7) == 7
    assert round_estimate(9) == 9
    # 10–99: nearest 10
    assert round_estimate(15) == 20
    assert round_estimate(42) == 40
    assert round_estimate(99) == 100
    # 100–999: nearest 50
    assert round_estimate(101) == 100
    assert round_estimate(155) == 150
    assert round_estimate(175) == 200
    assert round_estimate(999) == 1000
    # 1k–9.9k: nearest 100
    assert round_estimate(1234) == 1200
    assert round_estimate(2750) == 2800
    assert round_estimate(7733) == 7700
    # 10k–99k: nearest 1000
    assert round_estimate(12500) == 13000
    assert round_estimate(45499) == 45000
    # 100k+: nearest 10000
    assert round_estimate(123_456) == 120_000
    assert round_estimate(1_234_567) == 1_230_000


def test_fast_overhead_known_types():
    from poma_estimate.credit_math import apply_fast_overhead
    # PDF: +5%
    assert apply_fast_overhead(1000, "pdf") == 1050
    # DOCX: +30%
    assert apply_fast_overhead(1000, "docx") == 1300
    # XLSX: +25%
    assert apply_fast_overhead(1000, "xlsx") == 1250
    # Unknown ext: 1.0×
    assert apply_fast_overhead(1000, "txt") == 1000
    assert apply_fast_overhead(1000, "html") == 1000


def test_tokenizer_is_o200k_and_offline(monkeypatch, tmp_path):
    import poma_estimate.tokenizer as t
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")  # any download would fail
    t._encoder.cache_clear()
    assert t._encoder().name == "o200k_base"
    assert t.count_tokens("hello world") == 2
