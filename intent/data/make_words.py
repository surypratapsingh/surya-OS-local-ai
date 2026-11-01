#!/usr/bin/env python3
"""Write intent/data/words.txt once: the 5,000 most frequent lower-case words of
3-12 letters in the standard library sources of the Python that runs this.

Why this source: it is large, offline, and holds no NOVA test material (the
repository's own docs and logs quote test sentences, so they are not used).
The list is committed; the generator reads the committed file, so the
training data does not depend on which Python is installed later.

    python3 intent/data/make_words.py     (run with Python 3.13.18 for the committed list)
"""

from __future__ import annotations

import collections
from pathlib import Path
import re
import sysconfig

# Python keywords and builtins, and the words that trigger NOVA actions: a
# random span should not be made of them.
EXCLUDE = set("""and as assert async await break class continue def del elif else except false finally for from global
if import in is lambda none nonlocal not or pass raise return true try while with yield self cls args kwargs the
open play find search lock unlock mute unmute remind remember pause skip launch start create make write note notes
timer volume next previous louder quieter save locate resume stop run show bring set alarm ping music song songs
playlist file files document documents pdf docx txt word doc time date today""".split())


def main() -> None:
    stdlib = Path(sysconfig.get_paths()["stdlib"])
    counts: collections.Counter[str] = collections.Counter()
    for source in sorted(stdlib.glob("*.py")):
        text = source.read_text(encoding="utf-8", errors="ignore")
        counts.update(re.findall(r"(?<![A-Za-z_0-9])[a-z]{3,12}(?![A-Za-z_0-9])", text))
    words = [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if w not in EXCLUDE][:5000]
    out = Path(__file__).resolve().parent / "words.txt"
    out.write_text("\n".join(sorted(words)) + "\n", encoding="utf-8")
    print(f"wrote {len(words)} words to {out}")


if __name__ == "__main__":
    main()
