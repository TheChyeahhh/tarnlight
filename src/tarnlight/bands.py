"""Bands: per question name, escalate below E, review below R, auto at R and above; saved across sessions."""
import json, os
from pathlib import Path

DEFAULT = (40, 70)
PATH = Path.home() / ".tarnlight" / "bands.json"


class Bands:
    def __init__(self, path=PATH):
        self.path = Path(path)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}  # no file yet, or one that is not JSON: start from the defaults
        self.by_question = {q: tuple(v) for q, v in (saved.items() if isinstance(saved, dict) else ()) if _valid(v)}

    def __getitem__(self, question): return self.by_question.get(question, DEFAULT)

    def set(self, question, escalate, review):
        """Save the bands for one question. 0 <= escalate <= review <= 100, whole percents."""
        if not _valid([escalate, review]): raise ValueError("bands need 0 <= escalate <= review <= 100, whole numbers")
        self.by_question[question] = (int(escalate), int(review))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.by_question, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)  # a crash mid-write never leaves a half-written file


def _valid(v):
    return (isinstance(v, (list, tuple)) and len(v) == 2 and all(type(x) is int for x in v) and 0 <= v[0] <= v[1] <= 100)
