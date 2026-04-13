"""
utils/limbus_wiki.py — Limbus Company identity scraper and parser (V6).

Parser: V6 (transplanted from scripts/limbus_parser_v6.py)
Scraper: V1 (original download/cache infrastructure)

Provides:
- Data models (Identity, Skill, Defense, CoinEffect, SkillBonuses, Passive)
- Conditional text parser (bonus extraction from wiki description text)
- HTML parser (wiki HTML → structured Identity data)
- Scraper (MediaWiki API client, batch orchestration, JSON output)

Default output: assets/identities.json
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Data Models (unchanged from v1)
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass
class CoinEffect:
    """Per-coin effect data."""
    coin: int
    power_add: int = 0
    dmg_bonus: int = 0
    extra_hit_pct: int = 0
    reuse_count: int = 0             # Coin-level reuse ("Reuse this Coin N times")
    skill_reuse: bool = False        # V2: Skill-level reuse ("Reuse this Skill")
    dmg_bonus_on_crit: int = 0
    final_coin_dmg_bonus: int = 0
    is_unbreakable: bool = False
    is_conditional: bool = True
    ammo_cost: int = 0               # V4: "Spend N Ammo" cost for this coin
    ammo_type: str = ""              # V4: Ammo type name if non-generic
    applies_fragile: int = 0         # V4: Fragile stacks applied [On Hit]
    applies_type_fragility: str = "" # V4: Type fragility applied
    frequency_limit: int = 0         # V5: "(once per turn)" = 1, "(N times per turn)" = N
    frequency_limit_per_skill: int = 0  # V5: "(once per Skill)" = 1, "(N times per Skill)" = N
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "coin": self.coin,
            "power_add": self.power_add,
            "dmg_bonus": self.dmg_bonus,
            "extra_hit_pct": self.extra_hit_pct,
            "reuse_count": self.reuse_count,
            "is_conditional": self.is_conditional,
            "notes": self.notes,
        }
        if self.skill_reuse:
            d["skill_reuse"] = True
        if self.dmg_bonus_on_crit:
            d["dmg_bonus_on_crit"] = self.dmg_bonus_on_crit
        if self.is_unbreakable:
            d["is_unbreakable"] = True
        if self.ammo_cost:
            d["ammo_cost"] = self.ammo_cost
            if self.ammo_type:
                d["ammo_type"] = self.ammo_type
        if self.applies_fragile:
            d["applies_fragile"] = self.applies_fragile
        if self.applies_type_fragility:
            d["applies_type_fragility"] = self.applies_type_fragility
        if self.frequency_limit:
            d["frequency_limit"] = self.frequency_limit
        if self.frequency_limit_per_skill:
            d["frequency_limit_per_skill"] = self.frequency_limit_per_skill
        return d


@dataclass
class SkillBonuses:
    """Additive bonuses for a skill."""
    base_power_add: int = 0          # Base Power + Final Power + Skill Power (all equivalent)
    coin_power_add: int = 0
    skill_dmg_bonus: int = 0
    atk_weight_add: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base_power_add": self.base_power_add,
            "coin_power_add": self.coin_power_add,
            "skill_dmg_bonus": self.skill_dmg_bonus,
            "atk_weight_add": self.atk_weight_add,
            "notes": self.notes,
        }


BestCase = SkillBonuses


@dataclass
class Skill:
    """A single attack skill."""
    label: str
    name: str
    base_power: int
    coin_value: int
    num_coins: int
    damage_type: str
    sin_affinity: str = ""
    offense_level_offset: int = 0
    atk_weight: int = 1
    deck_count: int = 3
    is_minus_coin: bool = False
    has_unbreakable_coins: bool = False
    base_bonuses: SkillBonuses = field(default_factory=SkillBonuses)
    best_case: SkillBonuses = field(default_factory=SkillBonuses)
    coin_effects: list[CoinEffect] = field(default_factory=list)
    raw_conditionals: list[str] = field(default_factory=list)
    structured_conditionals: list[dict] = field(default_factory=list)  # V5

    def to_dict(self) -> dict:
        d = {
            "label": self.label,
            "name": self.name,
            "base_power": self.base_power,
            "coin_value": self.coin_value,
            "num_coins": self.num_coins,
            "damage_type": self.damage_type,
            "sin_affinity": self.sin_affinity,
            "offense_level_offset": self.offense_level_offset,
            "atk_weight": self.atk_weight,
            "deck_count": self.deck_count,
            "is_minus_coin": self.is_minus_coin,
            "has_unbreakable_coins": self.has_unbreakable_coins,
            "base_bonuses": self.base_bonuses.to_dict(),
            "best_case": self.best_case.to_dict(),
            "coin_effects": [ce.to_dict() for ce in self.coin_effects],
            "raw_conditionals": self.raw_conditionals,
        }
        if self.structured_conditionals:
            d["structured_conditionals"] = self.structured_conditionals
        return d


@dataclass
class Defense:
    """A defense skill.

    V3: Expanded with coin_effects, base_bonuses, best_case, raw_conditionals.
    Clashable Counters function as attack skills and should retain full data.
    """
    name: str
    defense_type: str
    base_power: int = 0
    coin_value: int = 0
    num_coins: int = 1
    offense_level_offset: int = 0
    damage_type: str = ""                                          # V3: Clashable Counters have damage types
    is_minus_coin: bool = False                                    # V3
    coin_effects: list[CoinEffect] = field(default_factory=list)   # V3
    base_bonuses: SkillBonuses = field(default_factory=SkillBonuses)  # V3
    best_case: SkillBonuses = field(default_factory=SkillBonuses)    # V3
    raw_conditionals: list[str] = field(default_factory=list)        # V3
    structured_conditionals: list[dict] = field(default_factory=list)  # V5

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "type": self.defense_type,
            "base_power": self.base_power,
            "coin_value": self.coin_value,
            "num_coins": self.num_coins,
            "offense_level_offset": self.offense_level_offset,
        }
        if self.damage_type:
            d["damage_type"] = self.damage_type
        if self.coin_effects:
            d["coin_effects"] = [ce.to_dict() for ce in self.coin_effects]
        if self.base_bonuses.notes or self.base_bonuses.base_power_add or self.base_bonuses.coin_power_add:
            d["base_bonuses"] = self.base_bonuses.to_dict()
        if self.best_case.notes or self.best_case.base_power_add or self.best_case.coin_power_add:
            d["best_case"] = self.best_case.to_dict()
        if self.raw_conditionals:
            d["raw_conditionals"] = self.raw_conditionals
        if self.structured_conditionals:
            d["structured_conditionals"] = self.structured_conditionals
        return d


@dataclass
class Passive:
    """V3: A passive skill (combat or support)."""
    name: str
    passive_type: str = ""    # "combat", "support", "owned"
    raw_text: str = ""        # Full raw text from the wiki

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.passive_type,
            "raw_text": self.raw_text,
        }


@dataclass
class Identity:
    """A complete identity."""
    name: str
    sinner: str
    skills: list[Skill] = field(default_factory=list)
    defense: Optional[Defense] = None
    defense2: Optional[Defense] = None
    passives: list[Passive] = field(default_factory=list)  # V3: Passive data
    flags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        slug = self.name.lower()
        slug = re.sub(r'[^a-z0-9\s-]', '', slug)
        slug = re.sub(r'[\s]+', '-', slug).strip('-')
        return slug

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "sinner": self.sinner,
            "skills": [s.to_dict() for s in self.skills],
            "defense": self.defense.to_dict() if self.defense else None,
            "flags": self.flags,
        }
        if self.defense2:
            d["defense2"] = self.defense2.to_dict()
        if self.passives:
            d["passives"] = [p.to_dict() for p in self.passives]
        return d


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Conditional Text Parser
# V2: Expanded _RESOURCE_RE, "Skill Power" synonym, cleaner raw_conditionals
# ═══════════════════════════════════════════════════════════════════════════════

# V6: Ammo type whitelist — parsed from https://limbuscompany.wiki.gg/wiki/Ammo
# Only resources in this set count as "ammo" for ammo_cost detection.
# "Spend N <resource>" where resource is NOT in this set is a status consumption, not ammo.
# This list should be updated when new ammo identities are added to the game.
_AMMO_TYPES: set[str] = {
    # Generic
    "ammo",
    # Identity-specific (from wiki Ammo page)
    "ammo - atelier logic",
    "the living & the departed",
    "the living",           # consumed separately
    "the departed",         # consumed separately
    "unjust enrichment",
    "magic bullet",
    "scorch propellant ammo",
    "scorch propellant",    # short form
    "tigermark round",
    "savage tigermark round",
    "lca fracture round",
    "arrow - shi",
    "arrow",                # short form
    "bullet - solitude",
    "spore round [base]",
    "spore round [buckshot]",
    "spore round base",     # without brackets
    "spore round buckshot", # without brackets
}


def _is_ammo_resource(resource_name: str) -> bool:
    """V6: Check if a resource name is a known ammo type."""
    return resource_name.lower().strip() in _AMMO_TYPES


# Can also fetch the ammo list from the wiki dynamically:
def fetch_ammo_types_from_wiki() -> set[str]:
    """Fetch the ammo type list from the wiki Ammo page. Returns lowercase names.

    Usage: call once during scraping to update _AMMO_TYPES.
    Not called during normal parsing (uses the hardcoded set above).
    """
    try:
        url = "https://limbuscompany.wiki.gg/api.php?action=parse&page=Ammo&prop=text&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "LimbusCalcScraper/2.0"})  # noqa: S310 — URL is hardcoded wiki API
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            data = json.loads(resp.read())
        html_text = data.get("parse", {}).get("text", {}).get("*", "")
        # Extract ammo names from the "Related Effects" table
        import html as _html
        text = _html.unescape(re.sub(r'<[^>]+>', ' ', html_text))
        text = re.sub(r'\s+', ' ', text)
        # Match rows: "Name Description Source" pattern
        ammo_names = set()
        for m in re.finditer(r'(?:Unique Ammo|Spent by certain|Some attacks cancel|Max Capacity)', text):
            # Look backward for the ammo name
            start = max(0, m.start() - 200)
            chunk = text[start:m.start()]
            # The name is typically the last capitalized phrase before the description
            name_m = re.search(r'([A-Z][A-Za-z\s\-\[\]&:.]+?)\s*(?:Unique Ammo|Spent by|Max (?:Capacity|Stack|Value)|- )', chunk)
            if name_m:
                ammo_names.add(name_m.group(1).strip().lower())
        return ammo_names
    except Exception as e:
        logger.warning(f"[LimbusWiki] Failed to fetch ammo types from wiki: {e}")
        return set()


# V2: Expanded resource/state keywords
_RESOURCE_RE = re.compile(
    r'\b(?:'
    # Condition words
    r'if|when|whenever|while|upon|after'
    # Stacking
    r'|for every|per'
    # Consume / Spend
    r'|consume|consuming|consumed|spend|spending|spent'
    # Status effects
    r'|Burn|Bleed|Sinking|Tremor|Poise|Charge|Rupture'
    r'|Nails|Paralysis|Paralyze|Bind|Fragility|Fragile|Protection'
    # Health/SP
    r'|HP|SP\b|sanity|missing HP|HP percentage'
    # Speed/tempo
    r'|Speed|Haste|faster|slower'
    # Resonance
    r'|Reson|Resonance'
    # Special resources
    r'|Ammo|Bullet|Fuel|Overheated|Insight|Discard'
    r'|Torn Memory|Bloodfeast|Hardblood'
    r'|Red Eyes|Coffin|Fanatic|Shield'
    r'|Clash Count|Clash Win|Clash Lose'
    r'|Defense Level Up|Defense Level Down'
    # V2: Identity-specific resources
    r'|Linebreaker|Strider|Serpent Arm'
    r'|Magic Bullet|Nerve Strike|Concentration'
    r'|Courier Trunk|negative effects|Deathrite|Bolus'
    r'|Talisman|Unjust Enrichment|Dullahan'
    r'|Grace of the Prescript|Unlock|Mark of the Prescript'
    r'|target has|on target|on self'
    # State conditions
    r'|killed|defeated|below|above|at least'
    r'|Stagger|stagger'
    r'|Defensive Stance'
    r'|survived|returned|substitut'
    r')\b',
    re.I
)


def _is_conditional_context(text: str, match: re.Match) -> bool:
    """Check if a matched bonus appears in text with resource/state keywords."""
    line_start = text.rfind('\n', 0, match.start()) + 1
    line_end = text.find('\n', match.end())
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]
    before = line[:match.start() - line_start]
    after = line[match.end() - line_start:]
    context = before + ' ' + after
    return bool(_RESOURCE_RE.search(context))


def _add(base: SkillBonuses, bc: SkillBonuses, field: str, value: int, conditional: bool):
    """Add value to best_case always. Add to base only if unconditional."""
    setattr(bc, field, getattr(bc, field) + value)
    if not conditional:
        setattr(base, field, getattr(base, field) + value)


class ConsumedTracker:
    """Tracks matched character ranges to prevent double-counting."""

    def __init__(self):
        self._ranges: list[tuple[int, int]] = []

    def mark(self, m: re.Match):
        self._ranges.append((m.start(), m.end()))

    def mark_range(self, start: int, end: int):
        self._ranges.append((start, end))

    def is_consumed(self, m: re.Match) -> bool:
        for cs, ce in self._ranges:
            if m.start() < ce and m.end() > cs:
                return True
        return False


def find_max_value(text: str, per_value: float) -> float:
    """Given text with '(max N%)' or '(max N)', find the total max."""
    pct = re.search(r'max\s*(\d+(?:\.\d+)?)\s*%', text, re.I)
    if pct:
        return float(pct.group(1))
    stacks = re.search(r'\(max\s*(\d+)\)', text, re.I)
    if stacks:
        return per_value * int(stacks.group(1))
    return per_value


def _consume_instead_blocks(text: str, ct: ConsumedTracker) -> dict:
    """Pre-scan for 'instead' patterns and consume both base AND replacement."""
    results = {'dmg': 0, 'coin_power': 0, 'atk_weight': 0, 'base_power': 0}

    for m in re.finditer(
        r'[Dd]eals?\s*\+\d+(?:\.\d+)?%\s*(?:more\s*)?damage\s*.{0,120}?instead\s*\((?:max\s*\d+\s*;\s*)?[Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['dmg'] += int(float(m.group(1)))
            ct.mark(m)
            _consume_preceding_damage(text, m.start(), ct)

    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage\s+instead(?!\s*\()',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['dmg'] += int(float(m.group(1)))
            ct.mark(m)
            _consume_preceding_damage(text, m.start(), ct)

    for m in re.finditer(
        r'Coin Power\s*\+\d+\s*(?:for every|per)\s*.{1,80}?instead\s*\((?:max\s*\d+\s*;\s*)?[Mm]ax\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['coin_power'] += int(m.group(1))
            ct.mark(m)
            _consume_preceding_coin_power(text, m.start(), ct)

    for m in re.finditer(r'Atk\s*Weight\s*\+(\d+)\s*instead', text, re.I):
        if not ct.is_consumed(m):
            results['atk_weight'] += int(m.group(1))
            ct.mark(m)
            _consume_preceding_atk_weight(text, m.start(), ct)

    return results


def _consume_preceding_damage(text: str, instead_pos: int, ct: ConsumedTracker):
    search_start = max(0, instead_pos - 200)
    region = text[search_start:instead_pos]
    last_match = None
    for m in re.finditer(
        r'[Dd]eals?\s*\+\d+(?:\.\d+)?%\s*(?:more\s*)?damage(?:\s*(?:for every|per).{0,80}?\([Mm]ax\s*\d+(?:\.\d+)?%?\))?',
        region, re.I
    ):
        last_match = m
    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


def _consume_preceding_coin_power(text: str, instead_pos: int, ct: ConsumedTracker):
    search_start = max(0, instead_pos - 300)
    region = text[search_start:instead_pos]
    last_match = None
    for m in re.finditer(r'Coin Power\s*\+\d+\s*(?:for every|per).{1,80}?\([Mm]ax\s*\d+\)', region, re.I):
        last_match = m
    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


def _consume_preceding_atk_weight(text: str, instead_pos: int, ct: ConsumedTracker):
    search_start = max(0, instead_pos - 300)
    region = text[search_start:instead_pos]
    last_match = None
    for m in re.finditer(r'Atk\s*Weight\s*\+\d+', region, re.I):
        last_match = m
    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


# V2: Regex to strip the stat-line header from raw_conditionals
_STAT_HEADER_RE = re.compile(
    r'^.*?Amt\.\s*x\d+\s*',
    re.DOTALL
)


def _strip_stat_header(text: str) -> str:
    """Remove the stat-line header (base+coin, OL, Atk Weight, Amt.) from description text.

    V2 FIX: The raw text passed to parse_skill_conditionals often starts with garbage like
    '3 + 4 Skull Crushing 62 (60+2) Atk Weight ⯀ Amt. x3 At 5+ Poise...'
    We strip everything up to and including 'Amt. xN' to get clean conditional text.
    """
    m = re.search(r'Amt\.\s*x\d+\s*', text)
    if m:
        return text[m.end():]
    # Fallback: try stripping up to the offense level marker
    m = re.search(r'\d+\s*\(60[+-]\d+\).*?(?=\b[A-Z])', text)
    if m:
        return text[m.end():]
    return text


def parse_skill_conditionals(text: str) -> tuple[SkillBonuses, SkillBonuses, list[str]]:
    """Parse skill-level conditionals from stripped description text.

    V2: Text is pre-cleaned by _strip_stat_header before entry.
    """
    base = SkillBonuses()
    bc = SkillBonuses()
    ct = ConsumedTracker()
    base_notes: list[str] = []
    bc_notes: list[str] = []

    raw_lines: list[str] = []
    for line in text.split('\n'):
        line = line.strip()
        if line and len(line) > 5:
            raw_lines.append(line)

    # ══ PHASE 0: "INSTEAD" PATTERNS ══
    instead = _consume_instead_blocks(text, ct)
    if instead['dmg']:
        _add(base, bc, 'skill_dmg_bonus', instead['dmg'], conditional=True)
        bc_notes.append(f"Dmg +{instead['dmg']}% (instead replacement)")
    if instead['coin_power']:
        _add(base, bc, 'coin_power_add', instead['coin_power'], conditional=True)
        bc_notes.append(f"Coin Power +{instead['coin_power']} (instead replacement)")
    if instead['atk_weight']:
        _add(base, bc, 'atk_weight_add', instead['atk_weight'], conditional=True)
        bc_notes.append(f"Atk Weight +{instead['atk_weight']} (instead replacement)")

    # ══ PHASE 1: COMPOUND PATTERNS ══

    for m in re.finditer(
        r'(?:Final|Base|Skill)\s*Power\s*\+(\d+)\s*and\s*deal\s*\+(\d+)%\s*(?:more\s*)?damage\s*(?:for every|per)\s*.{1,80}?\(max\s*(\d+)\s*;\s*max\s*(\d+)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'base_power_add', int(m.group(1)) * int(m.group(3)), conditional=True)
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(4))), conditional=True)
            bc_notes.append(f"Power +{m.group(1)}x{m.group(3)}, Dmg +{m.group(4)}%")
            ct.mark(m)

    for m in re.finditer(
        r'Coin Power\s*\+(\d+)\s*and\s*deal\s*\+(\d+)%\s*(?:more\s*)?damage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            _add(base, bc, 'coin_power_add', int(m.group(1)), conditional=cond)
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=cond)
            note = f"Coin Power +{m.group(1)}, Crit +{m.group(2)}%"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    for m in re.finditer(
        r'(?:Final|Base|Skill)\s*Power\s*\+(\d+)\s*and\s*deal\s*\+(\d+)%\s*(?:more\s*)?damage',
        text, re.I
    ):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            _add(base, bc, 'base_power_add', int(m.group(1)), conditional=cond)
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=cond)
            note = f"Power +{m.group(1)}, Dmg +{m.group(2)}%"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 2: FINAL/BASE/SKILL POWER ══
    # V2: Added "Skill" as synonym

    for m in re.finditer(
        r'(?:Final|Base|Skill)\s*Power\s*\+(\d+)\s*(?:for every|per)\s+.{1,80}?\(max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'base_power_add', int(m.group(2)), conditional=True)
            bc_notes.append(f"Power +{m.group(2)} max")
            ct.mark(m)

    for m in re.finditer(
        r'(?:Base|Final|Skill)\s*Power\s*\+(\d+)\s*(?:for every|per)\s*(\d+)\s*(?:Stack\s*)?consumed',
        text, re.I
    ):
        if not ct.is_consumed(m):
            per_value = int(m.group(1))
            per_count = int(m.group(2))
            consume_m = re.search(r'[Cc]onsume\s*(?:up to\s*)?(\d+)', text)
            if consume_m:
                consume_max = int(consume_m.group(1))
                total = per_value * (consume_max // per_count)
                _add(base, bc, 'base_power_add', total, conditional=True)
                bc_notes.append(f"Consume {consume_max} → Power +{total}")
            ct.mark(m)

    for m in re.finditer(r'(?:Final|Base|Skill)\s*Power\s*\+(\d+)', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(m.group(1))
            _add(base, bc, 'base_power_add', val, conditional=cond)
            note = f"Power +{m.group(1)}"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 3: COIN POWER ══

    for m in re.finditer(
        r'Coin Power\s*\+(\d+)\s*(?:for every|per)\s+.{1,80}?(?:\(|;\s*)max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'coin_power_add', int(m.group(2)), conditional=True)
            bc_notes.append(f"Coin Power +{m.group(2)} max")
            ct.mark(m)

    for m in re.finditer(
        r'[Cc]onsume\s*(?:up to\s*)?\d+.{0,60}?(?:to )?gain\s*(?:\+?(\d+)\s*Coin Power|Coin Power\s*\+(\d+))',
        text, re.I
    ):
        if not ct.is_consumed(m):
            val = int(m.group(1) or m.group(2))
            _add(base, bc, 'coin_power_add', val, conditional=True)
            bc_notes.append(f"Consume → Coin Power +{val}")
            ct.mark(m)

    for m in re.finditer(
        r'[Cc]onsume.{1,60}?Count.{1,60}?Coin Power\s*\((?:Coin Power\s*)?\+(\d+)\s*for every\s*(\d+).{0,40}?max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'coin_power_add', int(m.group(3)), conditional=True)
            bc_notes.append(f"Consume Count → Coin Power +{m.group(3)} max")
            ct.mark(m)

    for m in re.finditer(r'Coin Power\s*\+(\d+)', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(m.group(1))
            _add(base, bc, 'coin_power_add', val, conditional=cond)
            note = f"Coin Power +{m.group(1)}"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # V2: Also match "gain +N Coin Power" (inverted phrasing, e.g. Pirate Gregor S3)
    for m in re.finditer(r'[Gg]ain\s*(?:Coin Power\s*)?\+(\d+)\s*Coin Power', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(m.group(1))
            _add(base, bc, 'coin_power_add', val, conditional=cond)
            note = f"Coin Power +{m.group(1)}"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 4: DAMAGE % BONUSES ══

    # Formula notation: "deal +(variable x N)% damage (max Y%)"
    for m in re.finditer(
        r'[Dd]eals?\s*\+\s*\(.{1,60}?\)\s*%\s*(?:more\s*)?damage\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(1))), conditional=True)
            bc_notes.append(f"Dmg +{m.group(1)}% (formula max)")
            ct.mark(m)

    for m in re.finditer(
        r'[Dd]eals?\s*\+\s*\([^)]*?x\s*(\d+(?:\.\d+)?)\)\s*%\s*(?:more\s*)?damage',
        text, re.I
    ):
        if not ct.is_consumed(m):
            multiplier = float(m.group(1))
            consume_m = re.search(r'[Cc]onsume\s*(?:up to\s*)?(\d+)', text)
            if consume_m:
                consume_max = int(consume_m.group(1))
                total = int(consume_max * multiplier)
                _add(base, bc, 'skill_dmg_bonus', total, conditional=True)
                bc_notes.append(f"Dmg +{total}% (formula: {consume_max}x{multiplier})")
            ct.mark(m)

    for m in re.finditer(
        r'[Dd]eal\s*\+\s*\(\s*[^)]*?x\s*(\d+(?:\.\d+)?)\s*\)\s*%\s*(?:more\s*)?damage',
        text, re.I
    ):
        if not ct.is_consumed(m):
            multiplier = float(m.group(1))
            consume_m = re.search(r'[Cc]onsume\s*(?:up to\s*)?(\d+)', text)
            if consume_m:
                consume_max = int(consume_m.group(1))
                total = int(consume_max * multiplier)
                _add(base, bc, 'skill_dmg_bonus', total, conditional=True)
                bc_notes.append(f"Dmg +{total}% (formula: {consume_max}x{multiplier})")
            ct.mark(m)

    for m in re.finditer(
        r'\+(\d+(?:\.\d+)?)%\s*per\s*Clash\s*Count\s*,?\s*[Mm]ax\s*(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Clash dmg +{m.group(2)}% max")
            ct.mark(m)

    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage\s*(?:for every|per)\s*.{1,100}?\((?:max\s*\d+\s*;\s*)?max\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Dmg +{m.group(2)}% (stacking max)")
            ct.mark(m)

    # Crit stacking
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?\s*(?:for every|per)\s*.{1,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Crit dmg +{m.group(2)}% (stacking max)")
            ct.mark(m)

    # Crit flat — UNCONDITIONAL (crit always assumed)
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            val = int(float(m.group(1)))
            _add(base, bc, 'skill_dmg_bonus', val, conditional=False)
            note = f"Crit dmg +{m.group(1)}%"
            bc_notes.append(note)
            base_notes.append(note)
            ct.mark(m)

    # Missing HP
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*damage\s*(?:per|for\s+every)\s*1%\s*missing\s*HP.{0,50}?[Mm]ax\s*\+?(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"HP dmg +{m.group(2)}% max")
            ct.mark(m)

    for m in re.finditer(
        r'[Dd]eal\s*\+\(HP\s*percentage.{0,60}?\)%\s*damage\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(1))), conditional=True)
            bc_notes.append(f"HP% dmg +{m.group(1)}% max")
            ct.mark(m)

    # Scaling %
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*damage\s*(?:per|for\s+every).{0,60}?[Mm]ax\s*(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Scaling dmg +{m.group(2)}% max")
            ct.mark(m)

    # Flat damage %
    for m in re.finditer(r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(float(m.group(1)))
            _add(base, bc, 'skill_dmg_bonus', val, conditional=cond)
            note = f"Dmg +{m.group(1)}%"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # Standalone +X% damage
    for m in re.finditer(
        r'(?<!\.)(?<!\d)\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage(?!\s*(?:on\s*[Cc]rit|per|for every|dealt|as bonus|instead))',
        text
    ):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(float(m.group(1)))
            _add(base, bc, 'skill_dmg_bonus', val, conditional=cond)
            note = f"Dmg +{m.group(1)}%"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 5: ATTACK WEIGHT ══

    for m in re.finditer(
        r'Atk\s*Weight\s*\+(\d+)(?:\s*(?:for every|per).{1,60}?\([Mm]ax\s*\+?(\d+)\))?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            if m.group(2):
                val = int(m.group(2))
                _add(base, bc, 'atk_weight_add', val, conditional=True)
                bc_notes.append(f"Atk Weight +{m.group(2)} max")
            else:
                val = int(m.group(1))
                cond = _is_conditional_context(text, m)
                _add(base, bc, 'atk_weight_add', val, conditional=cond)
                note = f"Atk Weight +{m.group(1)}"
                bc_notes.append(note)
                if not cond:
                    base_notes.append(note)
            ct.mark(m)

    for m in re.finditer(
        r'gain\s*\+(\d+)\s*Atk\s*Weight(?:\s+(?:for every|per|for this).{1,60}?\([Mm]ax\s*\+?(\d+)\))?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            if m.group(2):
                val = int(m.group(2))
                _add(base, bc, 'atk_weight_add', val, conditional=True)
                bc_notes.append(f"Atk Weight +{m.group(2)} max")
            else:
                val = int(m.group(1))
                cond = _is_conditional_context(text, m)
                _add(base, bc, 'atk_weight_add', val, conditional=cond)
                note = f"Atk Weight +{m.group(1)}"
                bc_notes.append(note)
                if not cond:
                    base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 6: ATK WEIGHT DYNAMIC (V5 / P3-25) ══
    for m in re.finditer(
        r'Atk\s*Weight\s*becomes?\s*(?:equal\s*to\s*)?(.+?)(?:\s*[-\u2013]|\s*$)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            resource = m.group(1).strip()
            # Clean duplicated tooltip text: "Magic Bullet Magic Bullet" → "Magic Bullet"
            words = resource.split()
            half = len(words) // 2
            if half > 0 and words[:half] == words[half:2*half]:
                resource = ' '.join(words[:half])
            # Strip trailing lowercase artifact words from tooltip bleed
            resource = re.sub(r'\s+[a-z].*$', '', resource).strip()
            if resource:
                bc_notes.append(f"Atk Weight becomes {resource}")
            ct.mark(m)

    base.notes = base_notes
    bc.notes = bc_notes
    return base, bc, raw_lines


# ──────────────────────────────────────────────────────────────────────
# V5: Structured conditionals builder (P3-29)
# ──────────────────────────────────────────────────────────────────────

_TRIGGER_PREFIXES = [
    '[On Use]', '[Before Attack]', '[Before Use]', '[Combat Start]',
    '[Turn Start]', '[Turn End]', '[Attack End]', '[Strike End]',
    '[Skill End]', '[Clash Win]', '[Clash Lose]', '[Clash Start]',
    '[On Kill]', '[On Evade]', '[On Unopposed Attack]', '[Before Getting Hit]',
]

_DAMAGE_RELEVANT_RE = re.compile(
    r'(?:Final|Base|Skill|Coin)\s*Power\s*\+|'
    r'deal\s*\+\d+%\s*damage|'
    r'\+\d+%\s*(?:more\s*)?damage|'
    r'Atk\s*Weight\s*\+|'
    r'damage\s*on\s*[Cc]rit',
    re.I
)


def build_structured_conditionals(raw_lines: list[str]) -> list[dict]:
    """V5: Split raw conditional lines into structured entries with trigger and damage relevance."""
    results = []
    for line in raw_lines:
        # Split line at trigger prefix boundaries
        segments = []
        remaining = line
        while remaining:
            earliest_pos = len(remaining)
            earliest_prefix = None
            for prefix in _TRIGGER_PREFIXES:
                pos = remaining.find(prefix)
                if pos >= 0 and pos < earliest_pos:
                    # Don't split at position 0 (the line starts with this trigger)
                    if pos == 0:
                        earliest_pos = pos
                        earliest_prefix = prefix
                    elif pos > 0:
                        earliest_pos = pos
                        earliest_prefix = prefix

            if earliest_prefix and earliest_pos > 0:
                # Text before this trigger is its own segment
                before = remaining[:earliest_pos].strip()
                if before:
                    segments.append(("flat", before))
                remaining = remaining[earliest_pos:]
            elif earliest_prefix and earliest_pos == 0:
                # Find the next trigger to delimit this one
                next_pos = len(remaining)
                for prefix in _TRIGGER_PREFIXES:
                    pos = remaining.find(prefix, len(earliest_prefix))
                    if pos > 0 and pos < next_pos:
                        next_pos = pos
                segment_text = remaining[:next_pos].strip()
                trigger = earliest_prefix
                text_after = segment_text[len(trigger):].strip()
                segments.append((trigger, text_after))
                remaining = remaining[next_pos:]
            else:
                # No more triggers found
                remaining = remaining.strip()
                if remaining:
                    segments.append(("flat", remaining))
                break

        for trigger, text in segments:
            if not text:
                continue
            results.append({
                "trigger": trigger,
                "text": text,
                "is_damage_relevant": bool(_DAMAGE_RELEVANT_RE.search(text)),
            })

    return results


# V2: Expanded note-line prefix list for coin effects
_COIN_NOTE_PREFIXES = [
    '[On Hit]', '[Heads Hit]', '[Tails Hit]',       # V2: Added [Tails Hit]
    '[On Crit]', '[On Hit without Cracking]',
    '[Hit after Clash Win]',                          # V2: Added
    '[Coin Start]', '[Strike End]',                   # V2: Added [Strike End]
    '[Attack End]', '[Turn End]',
    '[Before Attack]', '[Before Use]',                # V2: Added
    '[Combat Start]', '[Turn Start]',                 # V2: Added
    '[Clash Win]', '[Clash Lose]',                    # V2: Added
    '[On Kill]', '[On Evade]',                        # V2: Added
    '+', 'Deal', 'deal', 'The final', 'This Coin',
    'Reuse', 'reuse',
    'Spend', 'spend',                                 # V2: Added
    'If ', 'At ', 'When ',                            # V2: Added "When"
    'Unbreakable',
]


def parse_coin_effects(text: str, coin_num: int, is_minus_coin: bool = False) -> CoinEffect:
    """Parse per-coin effects from text section for a specific coin.

    V4: Added is_minus_coin parameter for correct [Heads Hit]/[Tails Hit] conditionality.
    V4: Detects ammo cost, Fragile application, and type Fragility application.
    """
    ce = CoinEffect(coin=coin_num)
    ct = ConsumedTracker()
    has_conditional_bonus = False
    has_unconditional_bonus = False

    if 'Unbreakable Coin' in text:
        ce.is_unbreakable = True

    # V6: Detect ammo cost — validated against _AMMO_TYPES whitelist.
    # "Spend N Tremor Count" is NOT ammo. Only known ammo types count.
    ammo_m = re.search(r'[Ss]pend\s+(\d+)\s+(.+?)(?:\s+[Dd]eal|\s+\[|\s+[Oo]n\s|$)', text)
    if ammo_m:
        resource_name = ammo_m.group(2).strip()
        if _is_ammo_resource(resource_name):
            ce.ammo_cost = int(ammo_m.group(1))
            if resource_name.lower() != 'ammo':
                ce.ammo_type = resource_name

    # V4: Detect Fragile application ([On Hit] Inflict N Fragile)
    fragile_m = re.search(r'\[On Hit\].*?[Ii]nflict\s+(\d+)\s+Fragile', text)
    if fragile_m:
        ce.applies_fragile = int(fragile_m.group(1))

    # V4: Detect type Fragility application
    type_frag_m = re.search(r'\[On Hit\].*?[Ii]nflict\s+\d+\s+((?:Slash|Blunt|Pierce)\s+Fragility)', text)
    if type_frag_m:
        ce.applies_type_fragility = type_frag_m.group(1)

    # ══ "INSTEAD" PRE-SCAN ══
    instead = _consume_instead_blocks(text, ct)
    if instead['dmg']:
        ce.dmg_bonus += instead['dmg']
        has_conditional_bonus = True

    # ── Per-coin power ──
    m = re.search(
        r'(?:This Coin )?(?:gains? )?(?:Coin )?Power\s*\+(\d+).{0,100}?\(max\s*(\d+)',
        text, re.I
    )
    if m:
        ce.power_add = int(m.group(1)) * int(m.group(2))
        ct.mark(m)
        has_conditional_bonus = True

    if ce.power_add == 0:
        m = re.search(r'(?:This Coin )?(?:gains? )?Power\s*\+(\d+)', text, re.I)
        if m and not ct.is_consumed(m):
            ce.power_add = int(m.group(1))
            ct.mark(m)
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # ── Per-coin damage bonus ──

    # Crit stacking
    m = re.search(
        r'\+?\s*(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?'
        r'\s*(?:for every|per)\s*.{1,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    )
    if m and not ct.is_consumed(m):
        ce.dmg_bonus_on_crit = int(float(m.group(2)))
        ct.mark(m)
        has_conditional_bonus = True

    # Crit flat — UNCONDITIONAL
    if ce.dmg_bonus_on_crit == 0:
        m = re.search(
            r'\+?\s*(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?',
            text, re.I
        )
        if m and not ct.is_consumed(m):
            ce.dmg_bonus_on_crit = int(float(m.group(1)))
            ct.mark(m)
            has_unconditional_bonus = True

    # Stacking damage
    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage.{0,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            ce.dmg_bonus += int(float(m.group(2)))
            ct.mark(m)
            has_conditional_bonus = True

    # Final coin forward reference
    for m in re.finditer(r'(?:The |the )?final Coin deals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage', text, re.I):
        if not ct.is_consumed(m):
            ce.final_coin_dmg_bonus += int(float(m.group(1)))
            ct.mark(m)
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # Flat damage
    for m in re.finditer(r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage', text, re.I):
        if not ct.is_consumed(m):
            ce.dmg_bonus += int(float(m.group(1)))
            ct.mark(m)
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # Formula crit
    m = re.search(
        r'\([^)]*?\)\s*%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    )
    if m and not ct.is_consumed(m):
        ce.dmg_bonus_on_crit += int(float(m.group(1)))
        ct.mark(m)
        has_conditional_bonus = True

    # Standalone +X% Damage
    for m in re.finditer(
        r'(?<!\.)(?<!\d)\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage(?!\s*(?:on\s*[Cc]rit|per|for every|dealt|as bonus|instead))',
        text
    ):
        if not ct.is_consumed(m):
            ce.dmg_bonus += int(float(m.group(1)))
            ct.mark(m)
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # ── Extra hit ──
    m = re.search(
        r'[Dd]eal\s*(\d+(?:\.\d+)?)%\s*of\s*(?:damage dealt|this Coin.{0,30}?damage)\s*(?:as\s*bonus\s*damage)?(?:\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\))?',
        text, re.I
    )
    if m:
        if m.group(2):
            ce.extra_hit_pct = int(float(m.group(2)))
        else:
            ce.extra_hit_pct = int(float(m.group(1)))
        if _is_conditional_context(text, m):
            has_conditional_bonus = True
        else:
            has_unconditional_bonus = True

    # ── Reuse (V5: comprehensive detection) ──

    # Check for skill-level reuse first
    skill_reuse_m = re.search(r'[Rr]euse\s*(?:this\s*)?[Ss]kill', text, re.I)
    if not skill_reuse_m:
        # V5: Also match "[On Kill] Use this skill N more time(s)"
        skill_reuse_m = re.search(r'[Uu]se\s*this\s*[Ss]kill\s*(\d+)\s*more\s*time', text, re.I)

    if skill_reuse_m:
        ce.skill_reuse = True
        if _is_conditional_context(text, skill_reuse_m):
            has_conditional_bonus = True
        else:
            has_unconditional_bonus = True
    else:
        # Coin-level reuse detection — V5: expanded patterns
        reuse_found = False

        # "Reuse Coin/this Coin once (N times per Skill)" or "(N times max per Skill)" or "(N times max)"
        m = re.search(r'[Rr]euse\s*(?:this\s*)?[Cc]oin\s*(?:once\s*)?\((\d+)\s*times?\s*(?:max\s*)?(?:per\s*[Ss]kill|max)', text, re.I)
        if m:
            ce.reuse_count = int(m.group(1))
            ce.frequency_limit_per_skill = int(m.group(1))
            reuse_found = True
            has_conditional_bonus = True

        # "Reuse Coin (variable) times (N times max)" — variable with explicit cap
        if not reuse_found:
            m = re.search(r'[Rr]euse\s*(?:this\s*)?[Cc]oin\s*\([^)]*?\)\s*times?\s*\((\d+)\s*times?\s*max\)', text, re.I)
            if m:
                ce.reuse_count = int(m.group(1))
                reuse_found = True
                has_conditional_bonus = True

        # V5: "Reuse Coin (Resource - N) times" — variable WITHOUT explicit max
        # Insight caps at 5, so "(Insight - 1) times" = max 4
        if not reuse_found:
            m = re.search(r'[Rr]euse\s*(?:this\s*)?[Cc]oin\s*\(\s*(\w+)\s*-\s*(\d+)\s*\)\s*times?', text, re.I)
            if m:
                resource = m.group(1)
                offset = int(m.group(2))
                # Known resource caps
                resource_caps = {"Insight": 3, "Bright": 5}
                cap = resource_caps.get(resource, 3)  # default 3 if unknown
                ce.reuse_count = cap - offset
                reuse_found = True
                has_conditional_bonus = True

        # "Reuse Coin N times" / "Reuse ... N time(s)"
        if not reuse_found:
            m = re.search(r'[Rr]euse.*?(\d+)\s*time', text, re.I)
            if m:
                ce.reuse_count = int(m.group(1))
                reuse_found = True
                if _is_conditional_context(text, m):
                    has_conditional_bonus = True
                else:
                    has_unconditional_bonus = True

        # "Reuse Coin once" / "Reuse this Coin" / "reuse Coin once (once per Skill)"
        if not reuse_found:
            m = re.search(r'[Rr]euse\s*(?:this\s*)?[Cc]oin(?:\s*once)?', text, re.I)
            if m:
                ce.reuse_count = 1
                reuse_found = True
                has_conditional_bonus = True

        # V5: "use this Coin an additional time" (no "Reuse" keyword — Tingtang Hong Lu)
        if not reuse_found:
            m = re.search(r'[Uu]se\s*this\s*[Cc]oin\s*(?:an\s*)?additional\s*time', text, re.I)
            if m:
                ce.reuse_count = 1
                reuse_found = True
                has_conditional_bonus = True

        # V5: "reuse this Coin if it lands Heads (Up to N times)"
        if not reuse_found:
            m = re.search(r'[Rr]euse\s*this\s*[Cc]oin\s*if\s*.+?\([Uu]p\s*to\s*(\d+)\s*times?\)', text, re.I)
            if m:
                ce.reuse_count = int(m.group(1))
                reuse_found = True
                has_conditional_bonus = True

        # V5: Frequency limits on reuse — "(once per Skill)", "(1 time max)", "(once per turn)"
        if reuse_found:
            freq_m = re.search(r'\(once\s*per\s*[Ss]kill\)', text)
            if freq_m:
                ce.frequency_limit_per_skill = max(ce.frequency_limit_per_skill, 1)
            # "(N times max)" or "(N times max per Skill)"
            freq_m = re.search(r'\((\d+)\s*times?\s*max(?:\s*per\s*[Ss]kill)?\)', text)
            if freq_m and not ce.frequency_limit_per_skill:
                ce.frequency_limit_per_skill = int(freq_m.group(1))
            freq_m = re.search(r'\(once\s*per\s*turn\)', text)
            if freq_m:
                ce.frequency_limit = 1
            freq_m = re.search(r'\((\d+)\s*times?\s*per\s*turn\)', text)
            if freq_m:
                ce.frequency_limit = int(freq_m.group(1))

    # V5: General frequency limits — ONLY on coins with reuse.
    # A coin without reuse fires exactly once, so per-turn limits are meaningless for damage.
    # The "(once per turn)" on non-reuse coins is about status application caps, not damage.
    if ce.reuse_count > 0:
        if not ce.frequency_limit:
            freq_m = re.search(r'\((\d+)\s*times?\s*per\s*turn\)', text)
            if freq_m:
                ce.frequency_limit = int(freq_m.group(1))
            elif re.search(r'\(once\s*per\s*turn\)', text):
                ce.frequency_limit = 1
        if not ce.frequency_limit_per_skill:
            freq_m = re.search(r'\((\d+)\s*times?\s*per\s*[Ss]kill\)', text)
            if freq_m:
                ce.frequency_limit_per_skill = int(freq_m.group(1))
            elif re.search(r'\(once\s*per\s*[Ss]kill\)', text):
                ce.frequency_limit_per_skill = 1

    # ── Conditionality ──
    if has_conditional_bonus:
        ce.is_conditional = True
    elif has_unconditional_bonus:
        ce.is_conditional = False
    else:
        ce.is_conditional = False

    # ── Collect note lines ──
    # V4: Strip tab label artifacts + expanded prefix list
    for line in text.split('\n'):
        line = line.strip()
        line = re.sub(r'alt="[^"]*"', '', line)
        line = re.sub(r'src="[^"]*"', '', line)
        line = re.sub(r'(?:decoding|loading|width|height|data-file-\w+)="[^"]*"', '', line)
        line = re.sub(r'<img\s*/?>', '', line)
        line = re.sub(r'\s+', ' ', line).strip()

        # V4 Polish: Strip tab label artifacts from coin notes
        # These appear when section boundaries let "Skill 2 - 1" or "Defense" tab text leak in
        if re.match(r'^Skill\s+\d+(?:\s*-\s*\d+)?$', line):
            continue
        if re.match(r'^Defense(?:\s+\d+)?$', line):
            continue
        # Strip trailing tab labels that got appended to legitimate text
        # V4: Strip ALL trailing tab labels (there can be multiple chained, e.g. "Skill 2 - 1 Skill 2 - 2")
        line = re.sub(r'(?:\s+Skill\s+\d+(?:\s*-\s*\d+)?)+\s*$', '', line)
        line = re.sub(r'(?:\s+Defense(?:\s+\d+)?)+\s*$', '', line)

        if line and len(line) > 3 and not line.startswith('|'):
            if any(line.startswith(p) for p in _COIN_NOTE_PREFIXES) \
               or 'damage' in line.lower() or 'power' in line.lower():
                ce.notes.append(line)

    return ce


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: HTML Parser
# V2: Truncation fix, stat-header stripping
# ═══════════════════════════════════════════════════════════════════════════════


def strip_tooltips(text: str) -> str:
    """Remove tooltip-contents spans and all nested content."""
    result = []
    i = 0
    marker = 'class="tooltip-contents"'
    while i < len(text):
        idx = text.find(marker, i)
        if idx < 0:
            result.append(text[i:])
            break
        span_start = text.rfind('<span', i, idx)
        if span_start < 0:
            span_start = idx
        result.append(text[i:span_start])
        depth = 0
        j = span_start
        while j < len(text):
            if text[j:j+5] == '<span':
                depth += 1
            elif text[j:j+7] == '</span>':
                depth -= 1
                if depth == 0:
                    j += 7
                    break
            j += 1
        i = j
    return ''.join(result)


def strip_tags(text: str) -> str:
    """Remove HTML tags and leftover fragments."""
    cleaned = strip_tooltips(text)
    out = re.sub(r'<[^>]+>', ' ', cleaned)
    out = re.sub(r'<\w+[^>]*$', ' ', out, flags=re.MULTILINE)
    out = re.sub(r'<img[^>]*', ' ', out)
    out = re.sub(r'\s*/>', ' ', out)
    out = re.sub(r'\b(?:alt|src|decoding|loading|width|height|data-file-\w+|class|style)="[^"]*"', ' ', out)
    return re.sub(r'\s+', ' ', out).strip()


def clean_html(raw: str) -> str:
    """Decode HTML entities and strip tooltips."""
    text = html_lib.unescape(raw)
    text = strip_tooltips(text)
    return text


STAT_LINE_RE = re.compile(
    r'<b>(\d+)</b>\s*'
    r'(?:<img[^>]*alt="([^"]+)"[^>]*/?\s*>\s*)?'
    r'<b>([+-])\s*(\d+)</b>',
    re.DOTALL
)

NAME_RE = re.compile(r'skillgrad-font.*?<span[^>]*>(.*?)</span>', re.DOTALL)

SIN_FRAME_RE = re.compile(
    r'alt="(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)\d+(?:BG)?\.png"',
    re.I
)

SIN_LCB_RE = re.compile(
    r'alt="LcbSin(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)\.png"',
    re.I
)


# ══════════════════════════════════════════════════════════════════════════════
# SPECIAL-CASED IDENTITIES (kept from v1)
# ══════════════════════════════════════════════════════════════════════════════

def _build_firefist_gregor() -> Identity:
    """Firefist Office Survivor Gregor — semantic 'instead' patterns without keyword."""
    return Identity(
        name="Firefist Office Survivor Gregor",
        sinner="Gregor",
        skills=[
            Skill(
                label="S1", name="Flamethrow",
                base_power=3, coin_value=4, num_coins=2,
                damage_type="Blunt", sin_affinity="Wrath",
                offense_level_offset=2, atk_weight=1, deck_count=3,
                base_bonuses=SkillBonuses(),
                best_case=SkillBonuses(
                    coin_power_add=2,
                    notes=["Coin Power +2 max (per 3 Burn)"],
                ),
                coin_effects=[
                    CoinEffect(coin=1, is_conditional=False,
                               notes=["[On Hit] Inflict 2 Burn"]),
                    CoinEffect(coin=2, is_conditional=True,
                               notes=["[On Hit] Consume 15 Fuel to inflict +2 Burn Count"]),
                ],
                raw_conditionals=[
                    "Coin Power +1 for every 3 Burn on target (max 2)",
                ],
            ),
            Skill(
                label="S2", name="I'll burn away every last drop of your filthy blood",
                base_power=4, coin_value=6, num_coins=2,
                damage_type="Blunt", sin_affinity="Wrath",
                offense_level_offset=3, atk_weight=1, deck_count=2,
                base_bonuses=SkillBonuses(),
                best_case=SkillBonuses(
                    coin_power_add=2,
                    skill_dmg_bonus=30,
                    notes=["Coin Power +2 max (per 6 Burn)", "Dmg +30% (Overheated instead of +15%)"],
                ),
                coin_effects=[
                    CoinEffect(coin=1, is_conditional=False,
                               notes=["[On Hit] Inflict 2 Burn"]),
                    CoinEffect(coin=2, is_conditional=False,
                               notes=["[On Hit] Inflict +2 Burn Count"]),
                ],
                raw_conditionals=[
                    "Coin Power +1 for every 6 Burn on target (max 2)",
                    "Coins that consumed District 12 Fuel deal +15% damage",
                    "When in Overheated Fuel state, deal +30% damage instead",
                ],
            ),
            Skill(
                label="S3", name="Firefist",
                base_power=5, coin_value=4, num_coins=3,
                damage_type="Blunt", sin_affinity="Wrath",
                offense_level_offset=5, atk_weight=1, deck_count=1,
                base_bonuses=SkillBonuses(),
                best_case=SkillBonuses(
                    base_power_add=3,
                    coin_power_add=2,
                    skill_dmg_bonus=100,
                    notes=[
                        "Base Power +3 max (per 6 Burn)",
                        "Coin Power +2 max (per 3 Burn Count)",
                        "Dmg +100% (Overheated: +4% per Fuel consumed, max 100%)",
                    ],
                ),
                coin_effects=[
                    CoinEffect(coin=1, is_conditional=True,
                               notes=["[On Hit] If Fuel consumed, inflict 2 Burn + 1 Burn Count"]),
                    CoinEffect(coin=2, is_conditional=True,
                               notes=["[On Hit] If Fuel consumed, inflict 2 Burn + 1 Burn Count"]),
                    CoinEffect(coin=3, dmg_bonus=100, is_conditional=True,
                               notes=["[On Hit] +2% per Fuel consumed (max 50%); Overheated: +4% instead (max 100%)"]),
                ],
                raw_conditionals=[
                    "Coin Power +1 for every 3 Burn Count on target (max 2)",
                    "Base Power +1 for every 6 Burn on target (max 3)",
                    "Consume up to 25 District 12 Fuel and deal +2% damage for every Fuel consumed (max 50%)",
                    "When in Overheated Fuel state, deal +4% damage for every Fuel consumed (max 100%)",
                ],
            ),
        ],
        defense=Defense(
            name="I have to keep going for big sis",
            defense_type="Clashable Counter",
            base_power=9, coin_value=7, num_coins=1,
            offense_level_offset=2,
        ),
        flags=["HAS_CLASHABLE_DEFENSE"],
    )


def _build_ncorp_yisang_s3_2() -> Skill:
    """N Corp Yi Sang's S3-2 (Ryoshu's S4: I Shall Fire / Anytime)."""
    return Skill(
        label="S3-2", name="I Shall Fire / Anytime",
        base_power=4, coin_value=20, num_coins=2,
        damage_type="Pierce", sin_affinity="Gloom",
        offense_level_offset=3, atk_weight=1, deck_count=0,
        is_minus_coin=True,
        has_unbreakable_coins=True,
        base_bonuses=SkillBonuses(),
        best_case=SkillBonuses(
            coin_power_add=7,
            skill_dmg_bonus=0,
            notes=[
                "Coin Power +7 (per Torn Memory, max 7)",
            ],
        ),
        coin_effects=[
            CoinEffect(coin=1, is_conditional=False, is_unbreakable=True,
                        notes=["[On Hit] Inflict 3 Rupture + 2 Rupture Count"]),
            CoinEffect(coin=2, is_conditional=True, is_unbreakable=True,
                        dmg_bonus_on_crit=70,
                        notes=[
                            "+70% Damage on Critical Hit",
                            "[On Hit] Inflict 3 Rupture + 2 Rupture Count",
                        ]),
        ],
        raw_conditionals=[
            "Coin Power +1 for every 1 Torn Memory on self (max 7)",
            "Gain 1 Poise for every Torn Memory on self (max 7)",
        ],
    )


# ══════════════════════════════════════════════════════════════════════════════
# V3: DEDICATED SPECIAL CASES SECTION
# Identities that genuinely cannot be parsed generically.
# Each special case has a clear reason documented.
# ══════════════════════════════════════════════════════════════════════════════

# --- Full identity bypass (parser can't handle the HTML at all) ---
SPECIAL_CASE_IDENTITIES = {
    # Semantic "instead" pattern between Fuel/Overheated states without keyword
    "Firefist Office Survivor Gregor": _build_firefist_gregor,
}

# --- Post-parse fixups (parser gets most of it, we patch the rest) ---

_NCORP_YISANG_TITLE = "N Corp. E.G.O::Fell Bullet Yi Sang"
_MB_OUTIS_TITLE = "Lobotomy E.G.O::Magic Bullet Outis"
_NCORP_RYOSHU_TITLE = "N Corp. E.G.O::Contempt, Awe"  # partial match
_KK_RODION_TITLE = "Kurokumo Clan Wakashu Rodion"


def _postprocess_magic_bullet_outis(identity_dict: dict, raw_html: str):
    """V4.7: Parse Magic Bullet Outis S3 tier data directly from the wiki HTML.

    The wiki page has a section "The changes are as follows:" listing tiers like:
    "1-3 Magic Bullet ... - 15 Base Power, 4 Coin Power."
    "4-6 Magic Bullet ... - 17 Base Power, 6 Coin Power."
    "7 Magic Bullet ... - 30 Base Power, 10 Coin Power."

    We parse this rather than hardcoding.
    """
    # Strip HTML to get plain text
    text = html_lib.unescape(re.sub(r'<[^>]+>', ' ', raw_html))
    text = re.sub(r'\s+', ' ', text)

    tiers = {}
    # Pattern: "N-M Magic Bullet ... - X Base Power, Y Coin Power."
    # The tier number follows either "follows:" or a period+space from the previous tier.
    # Anchor: require tier number to follow sentence boundary or "follows:"
    for m in re.finditer(
        r'(?:follows:\s*|[.]\s+)(\d+(?:-\d+)?)\s+Magic Bullet.*?(\d+)\s+Base Power,\s*(\d+)\s+Coin Power',
        text
    ):
        tier_key = m.group(1)
        base = int(m.group(2))
        coin = int(m.group(3))
        tiers[tier_key] = {"base_power": base, "coin_value": coin}

    if not tiers:
        return  # Couldn't find tier data, leave as-is

    for skill in identity_dict.get("skills", []):
        if skill.get("label") == "S3":
            skill["magic_bullet_tiers"] = tiers
            skill["notes"] = skill.get("notes", []) + [
                "SPECIAL: Base power/coin power/atk weight vary by Magic Bullet count. "
                f"Tiers parsed from wiki: {tiers}"
            ]
            break


def _postprocess_ncorp_yisang(identity_dict: dict, all_identities: list[dict]):
    """V6: Rename N Corp Yi Sang's S3 to S3-1 and copy Ryoshu's parsed S4 as S3-2.

    NO HARDCODED DATA — copies the actual parsed skill from Ryoshu's identity.
    """
    for skill in identity_dict.get("skills", []):
        if skill.get("label") == "S3":
            skill["label"] = "S3-1"
            break

    # Find Ryoshu's S4 from her parsed data
    ryoshu_s4 = None
    for ident in all_identities:
        if "Contempt" in ident.get("name", "") and "Ry" in ident.get("name", ""):
            for skill in ident.get("skills", []):
                if skill.get("label") == "S4" and "I Shall Fire" in skill.get("name", ""):
                    ryoshu_s4 = skill
                    break
            break

    if ryoshu_s4:
        # Deep copy and relabel as S3-2 for Yi Sang
        import copy
        s3_2 = copy.deepcopy(ryoshu_s4)
        s3_2["label"] = "S3-2"
        identity_dict["skills"].append(s3_2)
    else:
        # Fallback: Ryoshu not parsed yet or S4 not found
        # This shouldn't happen if Ryoshu is parsed before Yi Sang (alphabetical order)
        pass

    if "HAS_UNBREAKABLE_COINS" not in identity_dict.get("flags", []):
        identity_dict.setdefault("flags", []).append("HAS_UNBREAKABLE_COINS")
    if "ALTERNATE_SKILL_FORMS" not in identity_dict.get("flags", []):
        identity_dict.setdefault("flags", []).append("ALTERNATE_SKILL_FORMS")


def _postprocess_ncorp_ryoshu(identity_dict: dict):
    """V6: Rename N Corp Ryoshu's 'Alt' skill to 'S4' for consistency.
    The parser already captures it from her wiki page — just needs the label fix.
    """
    for skill in identity_dict.get("skills", []):
        if skill.get("label") == "Alt" and "I Shall Fire" in skill.get("name", ""):
            skill["label"] = "S4"
            break


def _postprocess_kk_rodion(identity_dict: dict):
    """V6: Mark KK Rodion's Defense 2 as inert.
    PM added a phantom defense 2 for no functional reason.
    At 5+ Poise the counter becomes S3 — there is no real defense 2.
    Mark it so the calculator can't see it.
    """
    d2 = identity_dict.get("defense2")
    if d2:
        d2["inert"] = True
        d2["inert_reason"] = "Phantom defense — at 5+ Poise, counter becomes S3. Not a real defense skill."


def _postprocess_unfocused_volley(identity_dict: dict):
    """V6: Any skill with Unfocused Volley should be treated as Atk Weight 1.
    Unfocused Volley means each coin rolls against ONE target, not multiple.
    The stated atk_weight may be higher but functionally it's 1 for damage calculation.
    """
    for skill in identity_dict.get("skills", []):
        all_text = ' '.join(skill.get("raw_conditionals", []))
        for ce in skill.get("coin_effects", []):
            all_text += ' ' + ' '.join(ce.get("notes", []))
        if 'unfocused volley' in all_text.lower() or 'indiscriminate' in all_text.lower():
            if skill.get("atk_weight", 1) > 1:
                skill["atk_weight_original"] = skill["atk_weight"]
                skill["atk_weight"] = 1
                skill["atk_weight_note"] = "Unfocused Volley: treated as 1 for damage calculation"


def _postprocess_team_dependent(identity_dict: dict):
    """V6: Flag team-dependent bonuses as metadata.
    These are bonuses that require specific team composition and should not be prefilled.
    """
    team_keywords = [
        "Bolus Contamination", "Blade Lineage allies", "N Corp. allies",
        "Kurokumo Clan allies", "Liu Assoc. allies", "The Index",
        "Zwei Assoc. allies", "allies from", "ally with the earliest",
    ]
    for skill in identity_dict.get("skills", []):
        all_text = ' '.join(skill.get("raw_conditionals", []))
        for kw in team_keywords:
            if kw.lower() in all_text.lower():
                skill.setdefault("team_dependent_notes", []).append(
                    f"Contains team-dependent reference: '{kw}'"
                )
                break  # one flag per skill is enough


# V6: Unmatched pattern logging
_UNMATCHED_LOG: list[str] = []


def _log_unmatched_patterns(desc_text: str, identity_name: str, skill_label: str):
    """V6: Log conditional text that wasn't captured by any regex phase.
    Helps identify new Project Moon phrasings that need parser updates.
    """
    # Check for patterns that look like they should be captured but weren't
    potential = re.findall(
        r'(?:Power|damage|Atk Weight|Coin Power)\s*\+\d+',
        desc_text, re.I
    )
    # These would already be captured by the parser — if they appear in raw_conditionals
    # but NOT in base_bonuses/best_case notes, they were missed
    # For now, just log anything that has a bonus pattern
    if potential and len(desc_text) > 20:
        _UNMATCHED_LOG.append(f"{identity_name} {skill_label}: {desc_text[:200]}")


def _extract_passives(html: str) -> list[Passive]:
    """V3: Extract passive text from the HTML.

    Finds Combat Passive and Support Passive sections and extracts
    the raw text. Does not attempt structured parsing — stores raw text
    for downstream use.
    """
    passives = []

    # Find the passive section: text between "Combat Passive" and "Uptie"/"Trivia"/"Gallery"
    passive_start = re.search(r'(?:>|^)\s*Combat Passive', html)
    if not passive_start:
        return passives

    # Find where passives end
    end_markers = [
        re.search(r'(?:>|^)\s*(?:Uptie|Trivia|Gallery)\s*(?:<|$)', html[passive_start.start():]),
    ]
    passive_region_end = None
    for m in end_markers:
        if m:
            passive_region_end = passive_start.start() + m.start()
            break
    if not passive_region_end:
        passive_region_end = min(len(html), passive_start.start() + 20000)

    passive_html = html[passive_start.start():passive_region_end]

    # Find support passive boundary
    support_match = re.search(r'(?:>|^)\s*Support Passive', passive_html)

    # Combat passives region
    if support_match:
        combat_html = passive_html[:support_match.start()]
        support_html = passive_html[support_match.start():]
    else:
        combat_html = passive_html
        support_html = ""

    # Extract individual passive names and text from combat section
    # Passives typically have a name in a skillgrad-font div followed by description text
    combat_text = strip_tags(combat_html)
    combat_text = re.sub(r'^Combat Passive[s]?\s*', '', combat_text).strip()
    if combat_text:
        # Try to split by passive name patterns (they often have icon+name structure)
        # For now, store as one block — structured splitting can come later
        passives.append(Passive(
            name="Combat Passives",
            passive_type="combat",
            raw_text=combat_text[:3000],  # Cap to prevent bloat
        ))

    if support_html:
        support_text = strip_tags(support_html)
        support_text = re.sub(r'^Support Passive[s]?\s*', '', support_text).strip()
        if support_text:
            passives.append(Passive(
                name="Support Passive",
                passive_type="support",
                raw_text=support_text[:2000],
            ))

    return passives


def parse_identity(raw_html: str, title: str) -> Identity:
    """Parse a full identity page HTML into an Identity object."""
    html = clean_html(raw_html)
    sinner = identify_sinner(title)
    identity = Identity(name=title, sinner=sinner)

    if title in SPECIAL_CASE_IDENTITIES:
        return SPECIAL_CASE_IDENTITIES[title]()

    stat_matches = list(STAT_LINE_RE.finditer(html))
    if not stat_matches:
        identity.flags.append("NO_SKILL_DATA")
        return identity

    name_matches = list(NAME_RE.finditer(html))
    name_positions = [
        (m.start(), re.sub(r'<[^>]+>', '', m.group(1)).strip())
        for m in name_matches
    ]

    skills: list[Skill] = []
    for idx, match in enumerate(stat_matches):
        pos = match.start()
        next_pos = stat_matches[idx + 1].start() if idx + 1 < len(stat_matches) else len(html)
        next_pos = min(next_pos, pos + 15000)
        block = html[pos:next_pos]
        prev_start = stat_matches[idx - 1].end() if idx > 0 else max(0, pos - 5000)
        prefix = html[prev_start:pos]

        skill = _parse_skill_block(block, match, name_positions, pos, prefix)
        if skill:
            skills.append(skill)

    _assign_tab_labels(html, skills, stat_matches)
    _dedup_labels(skills)
    attack_skills, defense, defense2 = _separate_defense(skills)

    identity.skills = attack_skills
    identity.defense = defense
    identity.defense2 = defense2
    identity.passives = _extract_passives(html)  # V3: Extract passive text
    identity.flags = _detect_flags(html, attack_skills, defense, defense2)

    return identity


def _parse_skill_block(
    block: str,
    match: re.Match,
    name_positions: list[tuple[int, str]],
    block_global_pos: int,
    prefix: str = "",
) -> Skill | None:
    """Parse a single skill block starting from a stat line match."""

    base_power = int(match.group(1))
    damage_type_raw = match.group(2)
    coin_sign = match.group(3)
    coin_value_abs = int(match.group(4))

    ATTACK_TYPES = {"Slash.png": "Slash", "Blunt.png": "Blunt", "Pierce.png": "Pierce"}
    DEFENSE_IMGS = {"Evade.png", "Guard.png", "Counter.png", "Charge.png",
                    "Clashable Counter.png", "Clashable Guard.png"}
    if damage_type_raw in ATTACK_TYPES:
        damage_type = ATTACK_TYPES[damage_type_raw]
    elif damage_type_raw in DEFENSE_IMGS:
        # V3: Store the defense image type so _detect_defense_type can use it directly
        damage_type = "_def:" + damage_type_raw.replace('.png', '')
    elif damage_type_raw:
        damage_type = damage_type_raw.replace('.png', '')
    else:
        damage_type = ""
    coin_value = coin_value_abs if coin_sign == '+' else -coin_value_abs
    is_minus = coin_sign == '-'
    is_defense_img = damage_type_raw in DEFENSE_IMGS if damage_type_raw else False

    coin_imgs = re.findall(r'alt="(Coin(?:\s*-\s*Unbreakable)?\.png)"', block[:3000])
    num_coins = len(coin_imgs)
    if num_coins == 0:
        num_coins = 1

    has_unbreakable = any('Unbreakable' in c for c in coin_imgs)

    name = ""
    for npos, nname in name_positions:
        if npos > block_global_pos and npos < block_global_pos + len(block):
            name = nname
            break

    header_text = strip_tags(block[:3000])

    off_match = re.search(r'(\d+)\s*\(60([+-]\d+)\)', header_text)
    offense_offset = int(off_match.group(2)) if off_match else 0

    atk_weight = header_text[:300].count('\u2BC0')
    if atk_weight == 0:
        atk_weight = 1

    amt_match = re.search(r'Amt\.\s*x(\d+)', header_text)
    deck_count = int(amt_match.group(1)) if amt_match else -1

    sin = ""
    if prefix:
        all_sin = list(SIN_FRAME_RE.finditer(prefix))
        if all_sin:
            sin = all_sin[-1].group(1).capitalize()
    if not sin:
        sin_m = SIN_FRAME_RE.search(block[:2000])
        if sin_m:
            sin = sin_m.group(1).capitalize()

    # V2: Strip stat-line header before passing to conditional parser
    first_coin_effect = re.search(r'alt="CoinEffect\d+\.png"', block)
    desc_end = first_coin_effect.start() if first_coin_effect else len(block)
    desc_text = strip_tags(block[:desc_end])
    desc_text = _strip_stat_header(desc_text)  # V2 FIX
    base_bonuses, best_case, raw_conditionals = parse_skill_conditionals(desc_text)

    # V2: Remove 1500-char cap on coin effect parsing
    coin_effects = _parse_all_coin_effects(block, num_coins, is_minus_coin=is_minus)

    _resolve_final_coin_bonuses(coin_effects, num_coins)

    # V5: Detect skill-level reuse that targets specific coins
    # "Reuse the final Coin", "Reuse its last Coin" → set reuse on last coin
    _apply_skill_level_reuse(desc_text, coin_effects, num_coins)

    # V5: Build structured conditionals
    structured = build_structured_conditionals(raw_conditionals)

    pre_label = "Def" if is_defense_img else ""

    return Skill(
        label=pre_label,
        name=name or "Unknown",
        base_power=base_power,
        coin_value=coin_value,
        num_coins=num_coins,
        damage_type=damage_type,
        sin_affinity=sin,
        offense_level_offset=offense_offset,
        atk_weight=atk_weight,
        # V4.8: Keep -1 as "no Amt. field" (defense). Don't convert to 0.
        # 0 = Amt. x0 (alt form), -1 = no Amt. (defense). This distinction matters.
        deck_count=deck_count if deck_count >= 0 else -1,
        is_minus_coin=is_minus,
        has_unbreakable_coins=has_unbreakable,
        base_bonuses=base_bonuses,
        best_case=best_case,
        coin_effects=coin_effects,
        raw_conditionals=raw_conditionals,
        structured_conditionals=structured,
    )


def _resolve_final_coin_bonuses(coin_effects: list[CoinEffect], num_coins: int):
    """Accumulate 'final coin deals +X%' bonuses onto the actual last coin."""
    total_final_bonus = 0
    for ce in coin_effects:
        if ce.final_coin_dmg_bonus:
            total_final_bonus += ce.final_coin_dmg_bonus
            ce.final_coin_dmg_bonus = 0
    if total_final_bonus == 0:
        return
    last_ce = next((ce for ce in coin_effects if ce.coin == num_coins), None)
    if last_ce:
        last_ce.dmg_bonus += total_final_bonus
    else:
        coin_effects.append(CoinEffect(coin=num_coins, dmg_bonus=total_final_bonus))


def _apply_skill_level_reuse(desc_text: str, coin_effects: list[CoinEffect], num_coins: int):
    """V5: Detect skill-level reuse instructions and apply them to the correct coin.

    Patterns:
    - "Reuse the final Coin" / "Reuse its last Coin" → last coin gets reuse_count=1
    - "use Coin 1 and 2 an additional time" → coins 1 and 2 get reuse_count=1
    """
    # "Reuse the final Coin" / "Reuse its last Coin"
    if re.search(r'[Rr]euse\s*(?:the\s*)?(?:final|last)\s*[Cc]oin', desc_text):
        last_ce = next((ce for ce in coin_effects if ce.coin == num_coins), None)
        if last_ce and last_ce.reuse_count == 0:
            last_ce.reuse_count = 1
            last_ce.is_conditional = True
        elif not last_ce:
            coin_effects.append(CoinEffect(coin=num_coins, reuse_count=1, is_conditional=True))

    # "Reuse its last Coin" (alternate phrasing)
    elif re.search(r'[Rr]euse\s*its\s*last\s*[Cc]oin', desc_text):
        last_ce = next((ce for ce in coin_effects if ce.coin == num_coins), None)
        if last_ce and last_ce.reuse_count == 0:
            last_ce.reuse_count = 1
            last_ce.is_conditional = True

    # "use Coin 1 and 2 an additional time" (multi-coin, Shi S5 Ishmael)
    m = re.search(r'[Uu]se\s*[Cc]oin\s*(\d+)\s*and\s*(\d+)\s*an\s*additional\s*time', desc_text)
    if m:
        for coin_num in [int(m.group(1)), int(m.group(2))]:
            target_ce = next((ce for ce in coin_effects if ce.coin == coin_num), None)
            if target_ce and target_ce.reuse_count == 0:
                target_ce.reuse_count = 1
                target_ce.is_conditional = True
            elif not target_ce:
                coin_effects.append(CoinEffect(coin=coin_num, reuse_count=1, is_conditional=True))


def _parse_all_coin_effects(block: str, num_coins: int, is_minus_coin: bool = False) -> list[CoinEffect]:
    """Find all CoinEffect markers and parse each section.

    V2 FIX: Removed 1500-char cap. Uses structural boundaries instead.
    V3 FIX: For the last coin, find a structural end boundary to prevent
    defense section text from bleeding into coin notes.
    """
    effects = []
    markers = list(re.finditer(r'alt="CoinEffect(\d+)\.png"', block))

    # V3: Find where the coin effects section ends (before defense/tab/passive content)
    # Look for markers that indicate we've left the skill's coin area
    _end_markers = re.compile(
        r'(?:'
        r'alt="(?:Evade|Guard|Counter|Clashable Counter|Clashable Guard)\.png"'  # Defense type icons
        # V3 FIX: Only match "Defense" as a tab label (after > tag close), NOT inside
        # phrases like "Defense Level Down" or "Defense Power Up"
        r'|>\s*Defense\s*<'                                                       # Tab label: >Defense<
        r'|>\s*Defense\s+\d'                                                      # Tab label: >Defense 2<
        r'|>\s*Skill\s+\d+\s*<'                                                  # Tab label: >Skill 1<
        r'|Combat Passive'                                                        # Passives section
        r'|Support Passive'
        r'|skillgrad-font'                                                        # Next skill's name block
        r')'
    )

    # Find the earliest end-of-coin-section marker AFTER the last CoinEffect marker
    last_marker_end = markers[-1].end() if markers else 0
    coin_section_end = len(block)
    for m in _end_markers.finditer(block, last_marker_end):
        coin_section_end = m.start()
        break

    for i, marker in enumerate(markers):
        coin_num = int(marker.group(1))
        start = marker.start()
        if i + 1 < len(markers):
            end = markers[i + 1].start()
        else:
            # V3: Use structural boundary, not end of block
            end = coin_section_end
        section = strip_tags(block[start:end])
        ce = parse_coin_effects(section, coin_num, is_minus_coin=is_minus_coin)
        effects.append(ce)

    return effects


# ══════════════════════════════════════════════════════════════════════════════
# TAB LABEL ASSIGNMENT (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

def _assign_tab_labels(html: str, skills: list[Skill], stat_matches: list[re.Match]):
    ego_tabs = [(m.start(), f"S{m.group(1)}-{m.group(2)}")
                for m in re.finditer(r'>?\s*Skill\s+(\d+)\s*-\s*(\d+)\s*<?', html)]
    simple_tabs = [(m.start(), f"S{m.group(1)}")
                   for m in re.finditer(r'>?\s*Skill\s+(\d+)\s*(?!-)', html)]
    content_tabs = ego_tabs if ego_tabs else simple_tabs
    # V4.8: Use the tight regex for actual tab anchors, PLUS the old broad regex as fallback
    # The tight regex catches real tabs; the old one provides coverage for Counters that
    # rely on false-positive "Def" assignments (because they have attack damage types)
    tight_def = [(m.start(), "Def") for m in re.finditer(r'>\s*Defense(?:\s+\d+)?\s*</?\w', html)]
    if tight_def:
        content_tabs.extend(tight_def)
    else:
        # Fallback to old broad regex if tight finds nothing (shouldn't happen but safety)
        for m in re.finditer(r'>?\s*Defense\s*<?', html):
            content_tabs.append((m.start(), "Def"))

    if not content_tabs:
        _assign_labels_by_amt(skills)
        return

    content_tabs.sort(key=lambda x: x[0])
    tab_groups = []
    i = 0
    while i < len(content_tabs):
        group = [content_tabs[i]]
        while i + 1 < len(content_tabs) and content_tabs[i + 1][0] - content_tabs[i][0] < 500:
            i += 1
            group.append(content_tabs[i])
        tab_groups.append(group)
        i += 1

    skill_positions = [(stat_matches[i].start(), i) for i in range(len(skills))]
    for group in tab_groups:
        group_end = group[-1][0]
        gidx = tab_groups.index(group)
        next_start = tab_groups[gidx + 1][0][0] if gidx + 1 < len(tab_groups) else float('inf')
        following = [(p, si) for p, si in skill_positions if group_end < p < next_start]
        for j, (_, si) in enumerate(following):
            if j < len(group):
                skills[si].label = group[j][1]

    _assign_labels_by_amt([s for s in skills if not s.label])
    for s in skills:
        if not s.label:
            s.label = "Unknown"


def _dedup_labels(skills: list[Skill]):
    from collections import defaultdict
    by_label: dict[str, list[Skill]] = defaultdict(list)
    for s in skills:
        if s.label:
            by_label[s.label].append(s)
    for label, group in by_label.items():
        if len(group) <= 1:
            continue
        for s in group:
            if s.deck_count > 0:
                s.label = f"{label}-1"
            else:
                s.label = f"{label}-2"


def _assign_labels_by_amt(skills: list[Skill]):
    for s in skills:
        if s.label:
            continue
        if s.deck_count == 3:
            s.label = "S1"
        elif s.deck_count == 2:
            s.label = "S2"
        elif s.deck_count == 1:
            s.label = "S3"
        elif s.deck_count == 0:
            s.label = "Alt"
        elif s.deck_count == -1:
            s.label = "Def"


# ══════════════════════════════════════════════════════════════════════════════
# DEFENSE SEPARATION
# V3: Preserves full skill data on Defense, uses image alt for type detection
# ══════════════════════════════════════════════════════════════════════════════

def _separate_defense(
    skills: list[Skill],
) -> tuple[list[Skill], Defense | None, Defense | None]:
    attack = []
    defenses: list[Defense] = []

    for s in skills:
        is_def = False
        if s.label.startswith("Def"):
            is_def = True
        # V3: Check for _def: prefix in damage_type (set by image alt detection)
        if s.damage_type.startswith("_def:"):
            is_def = True
        # Override: skill with attack damage type AND deck_count > 0 is NOT defense.
        # deck_count > 0 is needed because Counters have attack damage types too,
        # but they have deck_count=-1 (no Amt. field).
        if is_def and not s.damage_type.startswith("_def:") and s.damage_type and s.deck_count > 0:
            is_def = False
            s.label = ""
        # V4.8: Also rescue deck_count=0 skills (alternate forms like Bow's Glimmer)
        # if they have an attack damage type. Counters have deck_count=-1 (converted to 0
        # by max(0, deck_count)), but their Amt. match returns -1 before conversion.
        # Alternate forms have Amt. x0 → deck_count=0 explicitly in the HTML.
        # The distinction: if the raw Amt. was "x0" it's an alt form, not a defense.
        if is_def and not s.damage_type.startswith("_def:") and s.damage_type and s.deck_count == 0:
            # This is an Amt. x0 skill with an attack damage type — it's an alternate attack form
            is_def = False
            s.label = ""
        if not s.damage_type and not is_def:
            is_def = True
        if s.name and not is_def and not s.damage_type:
            lower = s.name.lower()
            if any(k in lower for k in ['evade', 'guard', 'counter', 'charge', 'block', 'dodge', 'parry']):
                is_def = True

        if is_def:
            dtype = _detect_defense_type(s)
            # V3: Preserve full skill data on Defense
            # Clashable Counters have damage types from the skill they're clashing as
            real_damage_type = ""
            if s.damage_type.startswith("_def:"):
                pass  # defense image, no attack damage type
            elif s.damage_type:
                real_damage_type = s.damage_type

            defenses.append(Defense(
                name=s.name,
                defense_type=dtype,
                base_power=s.base_power,
                coin_value=s.coin_value,
                num_coins=s.num_coins,
                offense_level_offset=s.offense_level_offset,
                damage_type=real_damage_type,
                is_minus_coin=s.is_minus_coin,
                coin_effects=s.coin_effects,          # V3: preserved
                base_bonuses=s.base_bonuses,           # V3: preserved
                best_case=s.best_case,                 # V3: preserved
                raw_conditionals=s.raw_conditionals,   # V3: preserved
                structured_conditionals=s.structured_conditionals,  # V5
            ))
        else:
            attack.append(s)

    _assign_labels_by_amt(attack)

    def1 = defenses[0] if len(defenses) >= 1 else None
    def2 = defenses[1] if len(defenses) >= 2 else None
    return attack, def1, def2


def _detect_defense_type(skill: Skill) -> str:
    """V3: Uses raw_conditionals text first (catches Clashable), then image alt, then heuristics.

    Important: The stat-line image alt text only shows the BASE type (Guard.png, Counter.png).
    "Clashable Guard" and "Clashable Counter" appear in the SKILL TEXT, not the stat-line image.
    So we check text FIRST to catch the Clashable variants.
    """

    # Priority 1: Check raw_conditionals and coin notes for explicit Clashable markers
    all_text = ' '.join(skill.raw_conditionals)
    for ce in skill.coin_effects:
        all_text += ' ' + ' '.join(ce.notes)
    all_text_lower = all_text.lower()
    if 'clashable counter' in all_text_lower or '[clashable counter]' in all_text_lower:
        return "Clashable Counter"
    if 'clashable guard' in all_text_lower or '[clashable guard]' in all_text_lower:
        return "Clashable Guard"

    # Priority 2: Check image alt text (stored as _def:TypeName in damage_type)
    if skill.damage_type.startswith("_def:"):
        img_type = skill.damage_type[5:]
        if img_type == "Evade":
            return "Evade"
        if img_type == "Guard":
            return "Guard"
        if img_type == "Counter":
            return "Counter"
        if img_type == "Charge":
            return "Counter"

    # Priority 3: Name keywords
    name_lower = (skill.name or "").lower()
    if 'evade' in name_lower or 'dodge' in name_lower:
        return "Evade"
    if 'guard' in name_lower or 'block' in name_lower:
        return "Guard"
    if 'counter' in name_lower:
        return "Counter"
    if 'charge' in name_lower:
        return "Counter"

    # Last resort heuristic
    if skill.coin_value >= 8:
        return "Evade"
    if skill.base_power >= 10:
        return "Guard"
    return "Counter"


# ══════════════════════════════════════════════════════════════════════════════
# FLAGS (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

def _detect_flags(
    html: str,
    skills: list[Skill],
    defense: Defense | None,
    defense2: Defense | None,
) -> list[str]:
    flags = []
    if any(s.has_unbreakable_coins for s in skills):
        flags.append("HAS_UNBREAKABLE_COINS")
    # V3: Removed HAS_MINUS_COINS identity-level flag. Per-skill is_minus_coin is sufficient.
    if any(s.deck_count == 0 for s in skills):
        flags.append("HAS_CONDITIONAL_ONLY_SKILLS")
    if any(ce.reuse_count > 0 for s in skills for ce in s.coin_effects):
        flags.append("HAS_COIN_REUSE")
    if any(ce.skill_reuse for s in skills for ce in s.coin_effects):
        flags.append("HAS_SKILL_REUSE")  # V2: Distinguish skill reuse
    if any(ce.extra_hit_pct > 0 for s in skills for ce in s.coin_effects):
        flags.append("HAS_EXTRA_HIT")
    if any(re.search(r'S\d+-\d+', s.label) or s.label == 'Alt' for s in skills):
        flags.append("ALTERNATE_SKILL_FORMS")
    if defense2:
        flags.append("HAS_DUAL_DEFENSE")
    if any(s.atk_weight >= 2 for s in skills):
        flags.append("BASE_ATK_WEIGHT_2_PLUS")
    if defense and 'Clashable' in defense.defense_type:
        flags.append("HAS_CLASHABLE_DEFENSE")
    if 'Furioso' in html:
        flags.append("FURIOSO_SPECIAL_FORMULA")
    if re.search(r'final Coin deals \+', html, re.I):
        flags.append("HAS_FINAL_COIN_BONUS")
    if re.search(r'[Dd]iscard', html) and re.search(r'[Ii]nsight', html):
        flags.append("HAS_DISCARD_INSIGHT")
    if re.search(r'Ammo|Arrow - Shi', html):
        flags.append("HAS_AMMO_RESOURCE")
    return flags


# ══════════════════════════════════════════════════════════════════════════════
# SINNER IDENTIFICATION (unchanged from v1)
# ══════════════════════════════════════════════════════════════════════════════

_SINNERS = [
    "Yi Sang", "Faust", "Don Quixote", "Ryōshū", "Meursault",
    "Hong Lu", "Heathcliff", "Ishmael", "Rodion", "Sinclair",
    "Outis", "Gregor",
]

def identify_sinner(title: str) -> str:
    for sinner in _SINNERS:
        if title.endswith(sinner):
            return sinner
    for sinner in _SINNERS:
        if sinner in title:
            return sinner
    return "Unknown"




# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Scraper
# From api_client.py + scrape.py — MediaWiki API, caching, batch orchestration.
# ═══════════════════════════════════════════════════════════════════════════════

API_BASE = "https://limbuscompany.wiki.gg/api.php"
USER_AGENT = "LimbusCalcScraper/2.0 (discord bot project; not commercial)"
DELAY = 1.0

EXCLUDED_PREFIXES = ["Category:", "User:", "MediaWiki:", "Identities", "List of", "Template:"]


def api_request(params: dict) -> dict:
    """Make a MediaWiki API request. Falls back to curl if urllib is blocked."""
    params["format"] = "json"
    url = API_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 — URL is always API_BASE (hardcoded wiki)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Only fall back to curl for server errors (5xx); re-raise client errors (403, 404, etc.)
        if e.code < 500:
            raise
        import subprocess
        r = subprocess.run(  # noqa: S603 — args are hardcoded, only url varies (wiki API)
            ["curl", "-sS", "-L", "-A", USER_AGENT, "--max-time", "30", url],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"Both urllib and curl failed for: {url}") from e
        return json.loads(r.stdout)


def list_all_identities() -> list[str]:
    """Fetch all identity page titles from Category:Identities."""
    all_titles: list[str] = []
    cmcontinue: Optional[str] = None

    while True:
        params = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": "Category:Identities",
            "cmlimit": "500",
            "cmtype": "page",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue

        data = api_request(params)
        members = data.get("query", {}).get("categorymembers", [])
        all_titles.extend(m["title"] for m in members)

        if "continue" in data:
            cmcontinue = data["continue"]["cmcontinue"]
        else:
            break

    return [
        t for t in all_titles
        if not any(t.startswith(p) for p in EXCLUDED_PREFIXES) and t != "Identities"
    ]


def fetch_page_html(title: str) -> str:
    """Download the parsed HTML content of a wiki page via action=parse."""
    data = api_request({"action": "parse", "page": title, "prop": "text"})
    parse = data.get("parse", {})
    html = parse.get("text", {}).get("*", "")

    if not html:
        error = data.get("error", {})
        raise RuntimeError(f"No content for '{title}': {error.get('info', 'unknown')}")

    return html


# identify_sinner removed — use _identify_sinner from Section 3


class CachedClient:
    """API client with local HTML caching and rate limiting."""

    def __init__(self, cache_dir: Path, delay: float = DELAY):
        self.cache_dir = cache_dir
        self.delay = delay
        self._last_request = 0.0

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request = time.time()

    def _cache_path(self, title: str) -> Path:
        safe = title.replace("/", "_").replace("\\", "_").replace(":", " -")
        safe = safe.replace("?", "").replace("*", "").replace('"', "'")
        safe = safe.replace("<", "").replace(">", "").replace("|", "-")
        return self.cache_dir / f"{safe}.html"

    def get_html(self, title: str, resume: bool = False) -> str:
        """Get page HTML, using cache if resume=True and file exists."""
        cache_path = self._cache_path(title)

        if resume and cache_path.exists():
            return cache_path.read_text(encoding="utf-8")

        self._rate_limit()
        html = fetch_page_html(title)

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")

        return html

    def get_all_titles(self, sinner: Optional[str] = None) -> list[str]:
        """Get all identity titles, optionally filtered by sinner."""
        self._rate_limit()
        titles = list_all_identities()

        if sinner:
            sinner_lower = sinner.lower()
            titles = [t for t in titles if identify_sinner(t).lower() == sinner_lower]

        return titles

# ── Public API ──

CACHE_DIR = Path(os.path.join(config.ASSETS_PATH, os.pardir, "cache", "limbus", "wiki_pages"))
OUTPUT_PATH = Path(os.path.join(config.ASSETS_PATH, "identities.json"))


def _title_to_slug(title: str) -> str:
    """Convert a wiki page title to a URL-safe slug (matches Identity.id)."""
    slug = re.sub(r'[^a-z0-9\s-]', '', title.lower())
    return re.sub(r'[\s]+', '-', slug).strip('-')


def _run_postprocessors(identities: list[dict], html_cache: dict[str, str], log=None) -> None:
    """Run V6 postprocessors on parsed identity dicts.

    Args:
        identities: List of identity dicts (mutated in place).
        html_cache: Map of identity name → raw HTML (for Magic Bullet Outis).
        log: Optional progress logger.
    """
    _log = log or logger.info

    for identity in identities:
        name = identity.get("name", "")

        # Magic Bullet Outis: parse S3 tier data from HTML
        if _MB_OUTIS_TITLE in name:
            raw_html = html_cache.get(name, "")
            if raw_html:
                _postprocess_magic_bullet_outis(identity, raw_html)
                _log("  [postprocess] Magic Bullet Outis: S3 tiers parsed")

        # N Corp Ryoshu: rename Alt → S4
        if _NCORP_RYOSHU_TITLE in name:
            _postprocess_ncorp_ryoshu(identity)
            _log("  [postprocess] N Corp Ryoshu: Alt → S4")

        # KK Rodion: mark defense 2 as inert
        if _KK_RODION_TITLE in name:
            _postprocess_kk_rodion(identity)
            _log("  [postprocess] KK Rodion: defense 2 marked inert")

        # Unfocused Volley: treat as Atk Weight 1
        _postprocess_unfocused_volley(identity)

        # Team-dependent bonuses: flag as metadata
        _postprocess_team_dependent(identity)

    # N Corp Yi Sang: copy Ryoshu's parsed S4 as S3-2 (needs all identities)
    for identity in identities:
        if _NCORP_YISANG_TITLE in identity.get("name", ""):
            _postprocess_ncorp_yisang(identity, identities)
            _log("  [postprocess] N Corp Yi Sang: S3-2 linked")
            break


def download_all(*, redownload: bool = False, log=None) -> int:
    """Download all identity pages from the wiki into cache.

    Args:
        redownload: If True, fetch everything fresh. If False, skip cached pages.
        log: Optional callable for progress messages (e.g. logger.info).

    Returns:
        Number of pages downloaded (not cached hits).
    """
    _log = log or logger.info

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = CachedClient(CACHE_DIR)

    _log("Fetching identity list from wiki API...")
    titles = list_all_identities()
    _log(f"Found {len(titles)} identities")

    # Build set of expected cache paths so we can clean up stale files
    expected_paths: set[Path] = set()
    downloaded = 0
    for i, title in enumerate(titles, 1):
        sinner = identify_sinner(title)
        slug = _title_to_slug(title)
        cache_path = CACHE_DIR / sinner / f"{slug}.html"
        expected_paths.add(cache_path.resolve())

        if not redownload and cache_path.exists():
            continue

        client._rate_limit()
        html = fetch_page_html(title)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")
        downloaded += 1
        _log(f"  [{i:3d}/{len(titles)}] Downloaded: {title}")

    # Remove stale cache files that don't match current slug convention
    stale = [f for f in CACHE_DIR.rglob("*.html") if f.resolve() not in expected_paths]
    if stale:
        for f in stale:
            f.unlink()
        _log(f"Cleaned up {len(stale)} stale cache files from previous slug convention")

    _log(f"Download complete. {downloaded} pages fetched, {len(titles) - downloaded} cached.")
    return downloaded


def parse_all(*, log=None) -> list[dict]:
    """Parse ALL cached wiki pages into identity dicts.

    Always parses everything. No filtering. Writes assets/identities.json.

    Args:
        log: Optional callable for progress messages.

    Returns:
        List of identity dicts.
    """
    _log = log or logger.info

    html_files = sorted(CACHE_DIR.rglob("*.html"))
    if not html_files:
        _log(f"No cached pages found in {CACHE_DIR}. Run download_all() first.")
        return []

    _log(f"Parsing {len(html_files)} cached pages...")

    identities: list[dict] = []
    html_cache: dict[str, str] = {}  # name → raw HTML for postprocessors
    for i, fpath in enumerate(html_files, 1):
        # Title from filename slug → restore spaces for parser (lossy fallback)
        title = fpath.stem.replace('-', ' ')
        sinner = fpath.parent.name

        try:
            html = fpath.read_text(encoding="utf-8")

            # Extract canonical title from tab links in the wiki HTML.
            # action=parse returns only the article body (no <title> or <h1>),
            # but the Identity Story / Voicelines tab links contain the full page title.
            title_match = (
                re.search(r'title="([^"]+)/Identity Story"', html)
                or re.search(r'title="([^"]+)/Voicelines"', html)
            )
            if title_match:
                title = title_match.group(1).strip()

            identity = parse_identity(html, title)
            identities.append(identity.to_dict())
            html_cache[title] = html  # Cache for postprocessors

            n_skills = len(identity.skills)
            flags = identity.flags
            status = f"({n_skills} skills)"
            if flags:
                status += f" [{', '.join(flags[:2])}{'...' if len(flags) > 2 else ''}]"
            _log(f"  [{i:3d}/{len(html_files)}] {title} {status}")

        except Exception as e:
            _log(f"  [{i:3d}/{len(html_files)}] FAILED: {title}: {e}")
            logger.warning(f"[LimbusWiki] Failed to parse {title!r}: {e}")
            identities.append({
                "id": _title_to_slug(title),
                "name": title,
                "sinner": sinner,
                "skills": [],
                "defense": None,
                "flags": ["SCRAPE_FAILED", str(e)[:100]],
            })

    # V6 post-processing pipeline
    _run_postprocessors(identities, html_cache, log=_log)

    # Write output
    output = {
        "version": 11,
        "parser": "v6",
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "identity_count": len(identities),
        "identities": identities,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    total_skills = sum(len(ident.get("skills", [])) for ident in identities)
    failed = sum(1 for ident in identities if "SCRAPE_FAILED" in ident.get("flags", []))
    _log(f"Parse complete: {len(identities)} identities, {total_skills} skills, {failed} failed")
    _log(f"Output: {OUTPUT_PATH.resolve()}")

    return identities
