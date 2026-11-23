"""RTF estimator. Single page, body tokens from rtf2text."""

from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.tokenizer import count_tokens

RTF_EXTS = {"rtf"}


def estimate_rtf(path: Path) -> tuple[CreditBand, dict]:
    from striprtf.striprtf import rtf_to_text

    raw = path.read_text(encoding="utf-8", errors="replace")
    text = rtf_to_text(raw, errors="ignore")
    body_tokens = count_tokens(text)
    band = band_for(pages=1, tokens_low=body_tokens, tokens_expected=body_tokens, tokens_high=body_tokens)
    return band, {
        "kind": "rtf",
        "pages": 1,
        "char_count": len(raw),
        "body_tokens": body_tokens,
        "tokens_low": body_tokens,
        "tokens_expected": body_tokens,
        "tokens_high": body_tokens,
    }
