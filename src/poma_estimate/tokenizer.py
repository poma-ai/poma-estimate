"""Token counting via tiktoken o200k_base (the encoding POMA bills with).

The vocabulary file is bundled in `data/` and exposed to tiktoken as its
cache directory, so the tool works offline and never downloads anything.
"""

import os
from functools import lru_cache
from pathlib import Path

import tiktoken

_DATA = Path(__file__).parent / "data"


@lru_cache(maxsize=1)
def _encoder():
    if _DATA.is_dir():
        os.environ["TIKTOKEN_CACHE_DIR"] = str(_DATA)
    return tiktoken.get_encoding("o200k_base")


def count_tokens(text: str) -> int:
    if not text:
        return 0
    return len(_encoder().encode(text, disallowed_special=()))
