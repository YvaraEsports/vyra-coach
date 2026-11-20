"""Callouts verstehen: was die Spieler in den Comms sagen ("zwei Top Bridge, einer lit")
und wo das auf der Map ist (Minimap-Zone).

Callouts kommen aus drei Quellen:
  - von euch eingetragen (Web-UI Team-Tab, /coach callout, oder im Chat "merk dir …")
  - aus data/knowledge/maps.yaml
  - gelernt: Yvara AI erkennt in Reviews und beim Fortbilden (Caster, Minimap), welche Namen wo sind
"""
from __future__ import annotations

import re
import unicodedata

NUM_WORDS = {
    "ein": 1, "eins": 1, "einer": 1, "einen": 1, "one": 1, "solo": 1,
    "zwei": 2, "two": 2, "duo": 2, "drei": 3, "three": 3, "trio": 3, "vier": 4, "four": 4,
    "fünf": 5, "funf": 5, "five": 5, "full": 4, "alle": 4, "all": 4, "stack": 3,
}
STATUS = {
    "lit": "lit/angeschossen", "angeschossen": "lit/angeschossen", "getroffen": "lit/angeschossen", "hit": "lit/angeschossen",
    "low": "low", "one shot": "one shot", "oneshot": "one shot", "cracked": "lit/angeschossen", "broken": "lit/angeschossen",
    "tot": "down", "down": "down", "dead": "down", "killed": "down", "gekillt": "down", "weg": "down", "trade": "trade",
    "getradet": "trade", "traded": "trade",
}
MOVES = {
    "push": "push", "pusht": "push", "pushen": "push", "pushing": "push", "kommt": "push", "kommen": "push", "coming": "push",
    "rotiert": "rotate", "rotate": "rotate", "rotating": "rotate", "rotieren": "rotate", "rotation": "rotate",
    "flank": "flank", "flanke": "flank", "flankt": "flank", "hinter": "flank", "behind": "flank",
    "plant": "plant", "planted": "plant", "geplantet": "plant", "defuse": "defuse", "defused": "defuse",
    "spawn": "spawn", "flip": "spawn flip", "geflippt": "spawn flip", "flipped": "spawn flip",
    "hold": "hold", "halten": "hold", "hält": "hold", "anker": "hold", "anchor": "hold",
}
ASKS = {"hilfe": "needs help", "help": "needs help", "rein": "go in", "go": "go in", "los": "go in", "wait": "wait",
        "warte": "wait", "warten": "wait", "zurück": "fall back", "back": "fall back", "reset": "fall back",
        "uav": "uav", "streak": "streak"}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[^\wäöüß\s-]", " ", s)
    return re.sub(r"\s+", " ", s.replace("-", " ")).strip()


class CalloutIndex:
    """Schnelle Suche: welche Callouts kommen in einem Satz vor?"""

    def __init__(self, callouts: list[dict]) -> None:
        self.items = callouts
        self.phrases: list[tuple[str, dict]] = []
        for c in callouts:
            for p in [c["name"], *(c.get("aliases") or [])]:
                n = norm(p)
                if len(n) >= 2:
                    self.phrases.append((n, c))
        self.phrases.sort(key=lambda x: -len(x[0]))  # längste zuerst ("top bridge" vor "bridge")

    def find(self, text: str) -> list[dict]:
        n = f" {norm(text)} "
        found, used = [], []
        for p, c in self.phrases:
            for m in re.finditer(rf"(?<=\s){re.escape(p)}(?=\s)", n):
                span = (m.start(), m.end())
                if any(a < span[1] and span[0] < b for a, b in used):
                    continue
                used.append(span)
                if c not in found:
                    found.append(c)
        return found

    def legend(self, limit: int = 80) -> str:
        if not self.items:
            return ""
        parts = []
        for c in self.items[:limit]:
            s = c["name"] + (f" [{c['zone']}]" if c.get("zone") else "")
            if c.get("aliases"):
                s += " aka " + "/".join(c["aliases"][:3])
            if c.get("note"):
                s += f" ({c['note'][:60]})"
            parts.append(s)
        return "; ".join(parts)


def parse_call(text: str, index: CalloutIndex | None = None) -> dict:
    """Einen gesprochenen Call in Bausteine zerlegen (wer/wie viele/wo/Status/Bewegung)."""
    n = f" {norm(text)} "
    words = n.split()
    out: dict = {"where": [], "count": None, "status": [], "move": [], "ask": []}
    if index:
        out["where"] = [c["name"] + (f" [{c['zone']}]" if c.get("zone") else "") for c in index.find(text)]
    for w in words:
        if w.isdigit() and 0 < int(w) <= 5 and out["count"] is None:
            out["count"] = int(w)
        elif w in NUM_WORDS and out["count"] is None:
            out["count"] = NUM_WORDS[w]
    for table, key in ((STATUS, "status"), (MOVES, "move"), (ASKS, "ask")):
        for k, v in table.items():
            if re.search(rf"\s{re.escape(k)}\s", n) and v not in out[key]:
                out[key].append(v)
    return out


def describe(parsed: dict) -> str:
    bits = []
    if parsed.get("count"):
        bits.append(f"{parsed['count']}x")
    if parsed.get("where"):
        bits.append("@ " + ", ".join(parsed["where"]))
    for k in ("status", "move", "ask"):
        if parsed.get(k):
            bits.append(" ".join(parsed[k]))
    return " · ".join(bits)


def annotate(text: str, index: CalloutIndex | None) -> str:
    """Comms-Zeile mit Interpretation für die KI: 'zwei top bridge lit' -> '… ⟨2x · @ Top Bridge [C2] · lit⟩'."""
    d = describe(parse_call(text, index))
    return f"{text}  ⟨{d}⟩" if d else text
