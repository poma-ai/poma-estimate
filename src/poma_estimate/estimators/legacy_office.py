"""Legacy office formats: .xls (binary OLE), .xlsb (Excel binary)."""

import sys
from pathlib import Path

from poma_estimate.credit_math import CreditBand, band_for
from poma_estimate.tokenizer import count_tokens

LEGACY_OFFICE_EXTS = {"xls", "xlsb"}


def _estimate_xls(path: Path) -> tuple[CreditBand, dict]:
    import xlrd  # type: ignore[import-untyped]

    book = xlrd.open_workbook(str(path), on_demand=True, formatting_info=False, logfile=sys.stderr, verbosity=0)
    text_parts: list[str] = []
    sheet_count = 0
    for sheet_name in book.sheet_names():
        sheet = book.sheet_by_name(sheet_name)
        sheet_count += 1
        text_parts.append(f"# {sheet_name}")
        for row_idx in range(sheet.nrows):
            for val in sheet.row_values(row_idx):
                if val not in (None, ""):
                    text_parts.append(str(val))
        book.unload_sheet(sheet_name)
    body_tokens = count_tokens("\n".join(text_parts))
    band = band_for(pages=1, tokens_low=body_tokens, tokens_expected=body_tokens, tokens_high=body_tokens)
    return band, {
        "kind": "xls",
        "pages": 1,
        "sheets": sheet_count,
        "body_tokens": body_tokens,
        "tokens_low": body_tokens,
        "tokens_expected": body_tokens,
        "tokens_high": body_tokens,
    }


def _estimate_xlsb(path: Path) -> tuple[CreditBand, dict]:
    from pyxlsb import open_workbook  # type: ignore[import-untyped]

    text_parts: list[str] = []
    sheet_count = 0
    with open_workbook(str(path)) as wb:
        for sheet_name in wb.sheets:
            sheet_count += 1
            text_parts.append(f"# {sheet_name}")
            with wb.get_sheet(sheet_name) as sheet:
                for row in sheet.rows():
                    for cell in row:
                        val = cell.v
                        if val not in (None, ""):
                            text_parts.append(str(val))
    body_tokens = count_tokens("\n".join(text_parts))
    band = band_for(pages=1, tokens_low=body_tokens, tokens_expected=body_tokens, tokens_high=body_tokens)
    return band, {
        "kind": "xlsb",
        "pages": 1,
        "sheets": sheet_count,
        "body_tokens": body_tokens,
        "tokens_low": body_tokens,
        "tokens_expected": body_tokens,
        "tokens_high": body_tokens,
    }


def estimate_legacy_office(path: Path) -> tuple[CreditBand, dict]:
    ext = path.suffix.lstrip(".").lower()
    if ext == "xls":
        return _estimate_xls(path)
    if ext == "xlsb":
        return _estimate_xlsb(path)
    raise ValueError(f"unsupported legacy office ext: {ext}")
