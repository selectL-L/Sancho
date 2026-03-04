"""
utils/limbus.py — Limbus Company identity scraper and parser.

Consolidated from bundles/limbus/python/ into a single module.
Provides:
- Data models (Identity, Skill, Defense, CoinEffect, SkillBonuses)
- Conditional text parser (bonus extraction from wiki description text)
- HTML parser (wiki HTML → structured Identity data)
- Scraper (MediaWiki API client, batch orchestration, JSON output)

Default output: assets/identities.json
"""

from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Data Models
# From schema.py — dataclasses for Identity, Skill, Defense, CoinEffect, etc.
# ═══════════════════════════════════════════════════════════════════════════════



@dataclass
class CoinEffect:
    """Per-coin effect data."""
    coin: int                        # 1-indexed coin number
    power_add: int = 0               # Flat power added to this coin only
    dmg_bonus: int = 0               # +X% damage on this coin (additive pool)
    extra_hit_pct: int = 0           # "deal X% of damage as bonus damage" (separate instance)
    reuse_count: int = 0             # "Reuse this Coin N times"
    dmg_bonus_on_crit: int = 0       # "+X% Damage on Critical Hit" on this coin
    final_coin_dmg_bonus: int = 0    # "+X% to the final Coin" (accumulated onto last coin, not this one)
    is_unbreakable: bool = False     # This specific coin is Unbreakable
    is_conditional: bool = True      # True if bonus requires resource/state; False if on-hit/crit/flat
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
        if self.dmg_bonus_on_crit:
            d["dmg_bonus_on_crit"] = self.dmg_bonus_on_crit
        if self.is_unbreakable:
            d["is_unbreakable"] = True
        return d


@dataclass
class SkillBonuses:
    """Additive bonuses for a skill.

    Used for both base_bonuses (unconditional only) and best_case (everything maxed).
    Unconditional = on-hit, on-crit, flat with no resource/state requirement.
    """
    base_power_add: int = 0          # Sum of "Final Power +N" / "Base Power +N" maxes
    coin_power_add: int = 0          # Sum of "Coin Power +N" maxes
    skill_dmg_bonus: int = 0         # Sum of "+X% damage" maxes (goes into additive pool)
    atk_weight_add: int = 0          # Sum of "Atk Weight +N" maxes
    notes: list[str] = field(default_factory=list)  # Human-readable explanation of what was counted

    def to_dict(self) -> dict:
        return {
            "base_power_add": self.base_power_add,
            "coin_power_add": self.coin_power_add,
            "skill_dmg_bonus": self.skill_dmg_bonus,
            "atk_weight_add": self.atk_weight_add,
            "notes": self.notes,
        }


# Backward compat alias
BestCase = SkillBonuses


@dataclass
class Skill:
    """A single attack skill."""
    label: str                       # "S1", "S2", "S3", "S1-alt", "S3-alt", etc.
    name: str
    base_power: int
    coin_value: int                  # Signed: positive for plus coins, negative for minus
    num_coins: int
    damage_type: str                 # "Slash", "Pierce", "Blunt"
    sin_affinity: str = ""           # "Wrath", "Lust", "Sloth", "Gluttony", "Gloom", "Pride", "Envy"
    offense_level_offset: int = 0    # Added to base level (60) to get skill Offense Level
    atk_weight: int = 1
    deck_count: int = 3              # Amt. xN (how many copies in the 6-card deck)
    is_minus_coin: bool = False
    has_unbreakable_coins: bool = False
    base_bonuses: SkillBonuses = field(default_factory=SkillBonuses)  # Unconditional only (on-hit, crit, flat)
    best_case: SkillBonuses = field(default_factory=SkillBonuses)     # Everything maxed (superset of base_bonuses)
    coin_effects: list[CoinEffect] = field(default_factory=list)
    raw_conditionals: list[str] = field(default_factory=list)  # Full text of each conditional

    def to_dict(self) -> dict:
        return {
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


@dataclass
class Defense:
    """A defense skill (Evade, Guard, Counter, etc.)."""
    name: str
    defense_type: str                # "Evade", "Guard", "Counter", "Clashable Counter", "Clashable Guard"
    base_power: int = 0
    coin_value: int = 0
    num_coins: int = 1
    offense_level_offset: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.defense_type,
            "base_power": self.base_power,
            "coin_value": self.coin_value,
            "num_coins": self.num_coins,
            "offense_level_offset": self.offense_level_offset,
        }


@dataclass
class Identity:
    """A complete identity with all skills, defense, and metadata."""
    name: str                        # Full display name
    sinner: str                      # "Don Quixote", "Faust", etc.
    skills: list[Skill] = field(default_factory=list)
    defense: Optional[Defense] = None
    defense2: Optional[Defense] = None
    flags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """URL-safe slug from the identity name."""
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
        return d


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Conditional Text Parser
# From conditionals.py — 5-phase bonus extraction from wiki description text.
# ═══════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────
# Resource/state keywords that make a bonus conditional
# ──────────────────────────────────────────────────────────────────────

_RESOURCE_RE = re.compile(
    r'\b(?:'
    # Condition words
    r'if|when|whenever|while|upon|after'
    # Stacking
    r'|for every|per'
    # Consume
    r'|consume|consuming|consumed'
    # Status effects
    r'|Burn|Bleed|Sinking|Tremor|Poise|Charge|Rupture'
    r'|Nails|Paralysis|Bind|Fragility|Protection'
    # Health/SP
    r'|HP|SP\b|sanity'
    # Speed/tempo
    r'|Speed|Haste'
    # Resonance
    r'|Reson|Resonance'
    # Special resources
    r'|Ammo|Bullet|Fuel|Overheated|Insight|Discard'
    r'|Torn Memory|Bloodfeast|Hardblood'
    r'|Red Eyes|Coffin|Fanatic|Shield'
    r'|Clash Count|Clash Win|Clash Lose'
    r'|Defense Level Up'
    # State conditions
    r'|killed|defeated|below|above|at least'
    r'|Stagger|stagger'
    r'|Defensive Stance'
    r')\b',
    re.I
)


def _is_conditional_context(text: str, match: re.Match) -> bool:
    """Check if a matched bonus appears in text with resource/state keywords.

    Looks at the line containing the match. Strips out the matched text itself
    before checking, so patterns like "deal +20% damage" don't false-positive
    on the word "damage".
    """
    line_start = text.rfind('\n', 0, match.start()) + 1
    line_end = text.find('\n', match.end())
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end]

    # Remove the matched text to avoid self-triggering
    before = line[:match.start() - line_start]
    after = line[match.end() - line_start:]
    context = before + ' ' + after

    return bool(_RESOURCE_RE.search(context))


# ──────────────────────────────────────────────────────────────────────
# Dual-accumulator helper
# ──────────────────────────────────────────────────────────────────────

def _add(base: SkillBonuses, bc: SkillBonuses, field: str, value: int, conditional: bool):
    """Add value to best_case always. Add to base only if unconditional."""
    setattr(bc, field, getattr(bc, field) + value)
    if not conditional:
        setattr(base, field, getattr(base, field) + value)


class ConsumedTracker:
    """Tracks which character ranges in a text have been matched to prevent double-counting."""

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
    """Pre-scan for 'instead' patterns and consume both the base AND replacement lines.

    Returns a dict of what the "instead" replacement values contribute.
    This prevents the base patterns from also being counted.

    Pattern: "deal +X% damage ... (max A%)" followed by "deal +Y% damage instead (max B%)"
    Result: only B% counts, not A%+B%. We consume BOTH lines so neither is re-matched.
    """
    results = {'dmg': 0, 'coin_power': 0, 'atk_weight': 0, 'base_power': 0}

    # ── Damage "instead" ──
    # "deal +X% damage for every ... (max A%)" ... "deal +Y% damage ... instead (max B%)"
    for m in re.finditer(
        r'[Dd]eals?\s*\+\d+(?:\.\d+)?%\s*(?:more\s*)?damage\s*.{0,120}?instead\s*\((?:max\s*\d+\s*;\s*)?[Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['dmg'] += int(float(m.group(1)))
            ct.mark(m)
            # Also consume the base pattern that this replaces (look backward for the preceding damage line)
            _consume_preceding_damage(text, m.start(), ct)

    # "deal +X% damage instead" (flat, no max — the value IS the replacement)
    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage\s+instead(?!\s*\()',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['dmg'] += int(float(m.group(1)))
            ct.mark(m)
            _consume_preceding_damage(text, m.start(), ct)

    # ── Coin Power "instead" ──
    for m in re.finditer(
        r'Coin Power\s*\+\d+\s*(?:for every|per)\s*.{1,80}?instead\s*\((?:max\s*\d+\s*;\s*)?[Mm]ax\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['coin_power'] += int(m.group(1))
            ct.mark(m)
            _consume_preceding_coin_power(text, m.start(), ct)

    # ── Atk Weight "instead" ──
    for m in re.finditer(
        r'Atk\s*Weight\s*\+(\d+)\s*instead',
        text, re.I
    ):
        if not ct.is_consumed(m):
            results['atk_weight'] += int(m.group(1))
            ct.mark(m)
            _consume_preceding_atk_weight(text, m.start(), ct)

    return results


def _consume_preceding_damage(text: str, instead_pos: int, ct: ConsumedTracker):
    """Find and consume ONLY the closest damage pattern that the 'instead' replaces.

    Only consumes the single nearest preceding damage line, not all damage patterns
    in the area (which would incorrectly eat unrelated bonuses).
    """
    # Look backward up to 200 chars for the nearest damage pattern
    search_start = max(0, instead_pos - 200)
    region = text[search_start:instead_pos]

    # Find the LAST (closest) damage pattern in the region
    last_match = None
    for m in re.finditer(
        r'[Dd]eals?\s*\+\d+(?:\.\d+)?%\s*(?:more\s*)?damage(?:\s*(?:for every|per).{0,80}?\([Mm]ax\s*\d+(?:\.\d+)?%?\))?',
        region, re.I
    ):
        last_match = m

    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


def _consume_preceding_coin_power(text: str, instead_pos: int, ct: ConsumedTracker):
    """Only consumes the single nearest preceding coin power line (not all of them)."""
    search_start = max(0, instead_pos - 300)
    region = text[search_start:instead_pos]
    last_match = None
    for m in re.finditer(r'Coin Power\s*\+\d+\s*(?:for every|per).{1,80}?\([Mm]ax\s*\d+\)', region, re.I):
        last_match = m
    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


def _consume_preceding_atk_weight(text: str, instead_pos: int, ct: ConsumedTracker):
    """Only consumes the single nearest preceding atk weight line (not all of them)."""
    search_start = max(0, instead_pos - 300)
    region = text[search_start:instead_pos]
    last_match = None
    for m in re.finditer(r'Atk\s*Weight\s*\+\d+', region, re.I):
        last_match = m
    if last_match:
        ct.mark_range(search_start + last_match.start(), search_start + last_match.end())


def parse_skill_conditionals(text: str) -> tuple[SkillBonuses, SkillBonuses, list[str]]:
    """Parse skill-level conditionals from stripped description text.

    Returns (base_bonuses, best_case, raw_conditional_lines).
    - base_bonuses: only unconditional bonuses (on-hit, on-crit, flat with no resource)
    - best_case: ALL bonuses maxed (superset of base_bonuses)
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

    # ══ PHASE 0: "INSTEAD" PATTERNS (must be first to consume replaced patterns) ══
    # "Instead" patterns are ALWAYS conditional (they require a specific state)
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

    # "Final Power +N and deal +X% damage for every ... (max M; max Y%)" — CONDITIONAL (stacking)
    for m in re.finditer(
        r'(?:Final|Base)\s*Power\s*\+(\d+)\s*and\s*deal\s*\+(\d+)%\s*(?:more\s*)?damage\s*(?:for every|per)\s*.{1,80}?\(max\s*(\d+)\s*;\s*max\s*(\d+)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'base_power_add', int(m.group(1)) * int(m.group(3)), conditional=True)
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(4))), conditional=True)
            bc_notes.append(f"Final Power +{m.group(1)}x{m.group(3)}, Dmg +{m.group(4)}%")
            ct.mark(m)

    # "Coin Power +N and deal +X% damage on Crit" — if entire compound is gated behind a condition, both parts are conditional
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

    # "Final Power +N and deal +X% damage" (flat compound) — check context
    for m in re.finditer(
        r'(?:Final|Base)\s*Power\s*\+(\d+)\s*and\s*deal\s*\+(\d+)%\s*(?:more\s*)?damage',
        text, re.I
    ):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            _add(base, bc, 'base_power_add', int(m.group(1)), conditional=cond)
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=cond)
            note = f"Final Power +{m.group(1)}, Dmg +{m.group(2)}%"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 2: FINAL/BASE POWER ══

    # Stacking: "+N for every X ... (max M)" — CONDITIONAL (requires resource)
    for m in re.finditer(
        r'(?:Final|Base)\s*Power\s*\+(\d+)\s*(?:for every|per)\s+.{1,80}?\(max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'base_power_add', int(m.group(2)), conditional=True)
            bc_notes.append(f"Final/Base Power +{m.group(2)} max")
            ct.mark(m)

    # Consume-based with variable count — CONDITIONAL (requires consumed resource)
    for m in re.finditer(
        r'(?:Base|Final)\s*Power\s*\+(\d+)\s*(?:for every|per)\s*(\d+)\s*(?:Stack\s*)?consumed',
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
                bc_notes.append(f"Consume {consume_max} → Base Power +{total}")
            ct.mark(m)

    # Flat: "Final Power +N" / "Base Power +N" — check context
    for m in re.finditer(r'(?:Final|Base)\s*Power\s*\+(\d+)', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(m.group(1))
            _add(base, bc, 'base_power_add', val, conditional=cond)
            note = f"Final/Base Power +{m.group(1)}"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 3: COIN POWER ══

    # Stacking: "Coin Power +N for every X (max M)" or "; max M)" — CONDITIONAL
    for m in re.finditer(
        r'Coin Power\s*\+(\d+)\s*(?:for every|per)\s+.{1,80}?(?:\(|;\s*)max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'coin_power_add', int(m.group(2)), conditional=True)
            bc_notes.append(f"Coin Power +{m.group(2)} max")
            ct.mark(m)

    # Consume: "consume X to gain Coin Power +N" — CONDITIONAL
    for m in re.finditer(
        r'[Cc]onsume\s*(?:up to\s*)?\d+.{0,60}?(?:to )?gain\s*(?:\+?(\d+)\s*Coin Power|Coin Power\s*\+(\d+))',
        text, re.I
    ):
        if not ct.is_consumed(m):
            val = int(m.group(1) or m.group(2))
            _add(base, bc, 'coin_power_add', val, conditional=True)
            bc_notes.append(f"Consume → Coin Power +{val}")
            ct.mark(m)

    # Consume count-based — CONDITIONAL
    for m in re.finditer(
        r'[Cc]onsume.{1,60}?Count.{1,60}?Coin Power\s*\((?:Coin Power\s*)?\+(\d+)\s*for every\s*(\d+).{0,40}?max\s*(\d+)\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'coin_power_add', int(m.group(3)), conditional=True)
            bc_notes.append(f"Consume Count → Coin Power +{m.group(3)} max")
            ct.mark(m)

    # Flat: "Coin Power +N" — check context
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

    # Inverted: "gain +N Coin Power" — check context
    for m in re.finditer(r'gain\s*\+(\d+)\s*Coin Power', text, re.I):
        if not ct.is_consumed(m):
            cond = _is_conditional_context(text, m)
            val = int(m.group(1))
            _add(base, bc, 'coin_power_add', val, conditional=cond)
            note = f"Coin Power +{m.group(1)} (inverted)"
            bc_notes.append(note)
            if not cond:
                base_notes.append(note)
            ct.mark(m)

    # ══ PHASE 4: DAMAGE % BONUSES ══

    # Formula notation: "deal +(variable x N)% damage (max Y%)" — CONDITIONAL
    for m in re.finditer(
        r'[Dd]eals?\s*\+\s*\(.{1,60}?\)\s*%\s*(?:more\s*)?damage\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(1))), conditional=True)
            bc_notes.append(f"Dmg +{m.group(1)}% (formula max)")
            ct.mark(m)

    # Formula notation variant: "deal +(X x N)% damage" (no explicit max, compute from consume) — CONDITIONAL
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

    # Formula with "Deal +" prefix separated — CONDITIONAL
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

    # Clash-count damage: "+N% per Clash Count, Max M%" — CONDITIONAL
    for m in re.finditer(
        r'\+(\d+(?:\.\d+)?)%\s*per\s*Clash\s*Count\s*,?\s*[Mm]ax\s*(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Clash dmg +{m.group(2)}% max")
            ct.mark(m)

    # Stacking with max%: "deal +X% damage for every ... (max Y%)" — CONDITIONAL
    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage\s*(?:for every|per)\s*.{1,100}?\((?:max\s*\d+\s*;\s*)?max\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Dmg +{m.group(2)}% (stacking max)")
            ct.mark(m)

    # Crit stacking: "+X% damage on Crit for every ... (max Y%)" — CONDITIONAL (requires resource despite crit)
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?\s*(?:for every|per)\s*.{1,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Crit dmg +{m.group(2)}% (stacking max)")
            ct.mark(m)

    # Crit flat: "+X% Damage on Critical Hit" — UNCONDITIONAL (crit always assumed)
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

    # Missing HP: "+X% per 1% missing HP (max Y%)" — CONDITIONAL
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*damage\s*(?:per|for\s+every)\s*1%\s*missing\s*HP.{0,50}?[Mm]ax\s*\+?(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"HP dmg +{m.group(2)}% max")
            ct.mark(m)

    # HP percentage removed: "Deal +(HP percentage ... removed)% damage (max Y%)" — CONDITIONAL
    for m in re.finditer(
        r'[Dd]eal\s*\+\(HP\s*percentage.{0,60}?\)%\s*damage\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(1))), conditional=True)
            bc_notes.append(f"HP% dmg +{m.group(1)}% max")
            ct.mark(m)

    # Scaling: "+X% damage per/for every ... (max Y%)" — CONDITIONAL
    for m in re.finditer(
        r'\+?(\d+(?:\.\d+)?)%\s*damage\s*(?:per|for\s+every).{0,60}?[Mm]ax\s*(\d+(?:\.\d+)?)%',
        text, re.I
    ):
        if not ct.is_consumed(m):
            _add(base, bc, 'skill_dmg_bonus', int(float(m.group(2))), conditional=True)
            bc_notes.append(f"Scaling dmg +{m.group(2)}% max")
            ct.mark(m)

    # Flat: "deal +X% damage" — check context
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

    # Standalone: "+X% damage" (not followed by qualifiers) — check context
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

    # Standard: "Atk Weight +N (max M)" — check: if has "for every/per" it's conditional, else check context
    for m in re.finditer(
        r'Atk\s*Weight\s*\+(\d+)(?:\s*(?:for every|per).{1,60}?\([Mm]ax\s*\+?(\d+)\))?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            if m.group(2):
                # Has "for every/per (max M)" → CONDITIONAL (stacking)
                val = int(m.group(2))
                _add(base, bc, 'atk_weight_add', val, conditional=True)
                bc_notes.append(f"Atk Weight +{m.group(2)} max")
            else:
                # Flat — check context
                val = int(m.group(1))
                cond = _is_conditional_context(text, m)
                _add(base, bc, 'atk_weight_add', val, conditional=cond)
                note = f"Atk Weight +{m.group(1)}"
                bc_notes.append(note)
                if not cond:
                    base_notes.append(note)
            ct.mark(m)

    # Inverted: "gain +N Atk Weight" with optional max clause
    for m in re.finditer(
        r'gain\s*\+(\d+)\s*Atk\s*Weight(?:\s+(?:for every|per|for this).{1,60}?\([Mm]ax\s*\+?(\d+)\))?',
        text, re.I
    ):
        if not ct.is_consumed(m):
            if m.group(2):
                # Has max clause with stacking → CONDITIONAL
                val = int(m.group(2))
                _add(base, bc, 'atk_weight_add', val, conditional=True)
                bc_notes.append(f"Atk Weight +{m.group(2)} max (inverted)")
            else:
                # Flat inverted — check context
                val = int(m.group(1))
                cond = _is_conditional_context(text, m)
                _add(base, bc, 'atk_weight_add', val, conditional=cond)
                note = f"Atk Weight +{m.group(1)} (inverted)"
                bc_notes.append(note)
                if not cond:
                    base_notes.append(note)
            ct.mark(m)

    base.notes = base_notes
    bc.notes = bc_notes
    return base, bc, raw_lines


def parse_coin_effects(text: str, coin_num: int) -> CoinEffect:
    """Parse per-coin effects from the text section for a specific coin.

    `text` is the stripped text between this CoinEffect marker and the next.
    Sets is_conditional based on whether the primary bonuses require resources/state.
    """
    ce = CoinEffect(coin=coin_num)
    ct = ConsumedTracker()
    has_conditional_bonus = False
    has_unconditional_bonus = False

    if 'Unbreakable Coin' in text:
        ce.is_unbreakable = True

    # ══ "INSTEAD" PRE-SCAN for per-coin ══
    instead = _consume_instead_blocks(text, ct)
    if instead['dmg']:
        ce.dmg_bonus += instead['dmg']
        has_conditional_bonus = True  # "instead" always requires state

    # ── Per-coin power ──
    m = re.search(
        r'(?:This Coin )?(?:gains? )?(?:Coin )?Power\s*\+(\d+).{0,100}?\(max\s*(\d+)',
        text, re.I
    )
    if m:
        ce.power_add = int(m.group(1)) * int(m.group(2))
        ct.mark(m)
        has_conditional_bonus = True  # stacking = conditional

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

    # Crit stacking — CONDITIONAL (resource required despite crit)
    m = re.search(
        r'\+?\s*(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?'
        r'\s*(?:for every|per)\s*.{1,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    )
    if m and not ct.is_consumed(m):
        ce.dmg_bonus_on_crit = int(float(m.group(2)))
        ct.mark(m)
        has_conditional_bonus = True

    # Crit flat — UNCONDITIONAL (crit always assumed)
    if ce.dmg_bonus_on_crit == 0:
        m = re.search(
            r'\+?\s*(\d+(?:\.\d+)?)%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?',
            text, re.I
        )
        if m and not ct.is_consumed(m):
            ce.dmg_bonus_on_crit = int(float(m.group(1)))
            ct.mark(m)
            has_unconditional_bonus = True

    # Stacking: "deals +X% damage ... (max Y%)" — check each match
    for m in re.finditer(
        r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage.{0,100}?\([Mm]ax\s*(?:\d+\s*;\s*[Mm]ax\s*)?(\d+(?:\.\d+)?)%?\)',
        text, re.I
    ):
        if not ct.is_consumed(m):
            ce.dmg_bonus += int(float(m.group(2)))
            ct.mark(m)
            has_conditional_bonus = True  # stacking = conditional

    # "The final Coin deals +X% damage" — applies to LAST coin, not this one
    for m in re.finditer(r'(?:The |the )?final Coin deals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage', text, re.I):
        if not ct.is_consumed(m):
            ce.final_coin_dmg_bonus += int(float(m.group(1)))
            ct.mark(m)
            # Conditionality depends on context of the original coin
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # Flat damage: "deal +X% damage" — check context
    for m in re.finditer(r'[Dd]eals?\s*\+(\d+(?:\.\d+)?)%\s*(?:more\s*)?damage', text, re.I):
        if not ct.is_consumed(m):
            ce.dmg_bonus += int(float(m.group(1)))
            ct.mark(m)
            if _is_conditional_context(text, m):
                has_conditional_bonus = True
            else:
                has_unconditional_bonus = True

    # Formula crit: "(variable)% damage on Crit (max N%)" — CONDITIONAL (requires resource)
    m = re.search(
        r'\([^)]*?\)\s*%\s*(?:more\s*)?[Dd]amage\s*on\s*[Cc]rit(?:ical)?(?:\s*[Hh]it)?\s*\([Mm]ax\s*(\d+(?:\.\d+)?)%?\)',
        text, re.I
    )
    if m and not ct.is_consumed(m):
        ce.dmg_bonus_on_crit += int(float(m.group(1)))
        ct.mark(m)
        has_conditional_bonus = True

    # Standalone: "+X% Damage" (without "deal", e.g., "+10% Damage", "+30% Damage to targets with") — check context
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

    # ── Reuse ──
    m = re.search(r'[Rr]euse.*?(\d+)\s*time', text, re.I)
    if m:
        ce.reuse_count = int(m.group(1))
        if _is_conditional_context(text, m):
            has_conditional_bonus = True
        else:
            has_unconditional_bonus = True
    elif re.search(r'[Rr]euse\s*(?:this\s*)?[Cc]oin', text, re.I):
        ce.reuse_count = 1
        has_conditional_bonus = True  # conditional reuse (usually requires a state)

    # ── Determine overall conditionality ──
    # If the coin has ANY resource-dependent bonus, mark as conditional.
    # The engine will still use dmg_bonus_on_crit from conditional coins (crit is always unconditional).
    if has_conditional_bonus:
        ce.is_conditional = True
    elif has_unconditional_bonus:
        ce.is_conditional = False
    else:
        # No bonuses at all — default to unconditional (it's just a plain coin)
        ce.is_conditional = False

    # ── Collect note lines ──
    for line in text.split('\n'):
        line = line.strip()
        line = re.sub(r'alt="[^"]*"', '', line)
        line = re.sub(r'src="[^"]*"', '', line)
        line = re.sub(r'(?:decoding|loading|width|height|data-file-\w+)="[^"]*"', '', line)
        line = re.sub(r'<img\s*/?>', '', line)
        line = re.sub(r'\s+', ' ', line).strip()

        if line and len(line) > 3 and not line.startswith('|'):
            if any(line.startswith(p) for p in [
                '[On Hit]', '[Heads Hit]', '[On Crit]', '[On Hit without Cracking]',
                '[Coin Start]', '[Attack End]', '[Turn End]',
                '+', 'Deal', 'deal', 'The final', 'This Coin',
                'Reuse', 'reuse', 'If ', 'At ', 'Unbreakable',
            ]) or 'damage' in line.lower() or 'power' in line.lower():
                ce.notes.append(line)

    return ce


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: HTML Parser
# From html_parser.py — parse raw wiki HTML into structured Identity data.
# ═══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# HTML CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

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
    """Remove HTML tags and leftover fragments, preserve text content."""
    cleaned = strip_tooltips(text)
    # Remove full tags
    out = re.sub(r'<[^>]+>', ' ', cleaned)
    # Remove incomplete/broken tags (e.g., <img ... without closing >)
    out = re.sub(r'<\w+[^>]*$', ' ', out, flags=re.MULTILINE)
    out = re.sub(r'<img[^>]*', ' ', out)
    # Remove stray closing fragments: /> or >
    out = re.sub(r'\s*/>', ' ', out)
    # Remove leftover HTML attribute fragments
    out = re.sub(r'\b(?:alt|src|decoding|loading|width|height|data-file-\w+|class|style)="[^"]*"', ' ', out)
    return re.sub(r'\s+', ' ', out).strip()


def clean_html(raw: str) -> str:
    """Decode HTML entities and strip tooltips."""
    text = html_lib.unescape(raw)
    text = strip_tooltips(text)
    return text


# ══════════════════════════════════════════════════════════════════════════════
# SKILL BLOCK EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

# Matches: <b>5</b> <img alt="Slash.png"> <b>+ 3</b>  (plus coins, attack skill)
# AND:     <b>12</b> <img alt="Blunt.png"> <b>- 4</b>  (minus coins)
# AND:     <b>3</b> <img alt="Evade.png"> <b>+ 10</b>  (defense skill with defense type image)
# AND:     <b>10</b> <b>+ 3</b>                        (defense, no image at all)
# Group 2 captures the image alt text (may be damage type OR defense type OR None)
STAT_LINE_RE = re.compile(
    r'<b>(\d+)</b>\s*'
    r'(?:<img[^>]*alt="([^"]+)"[^>]*/?\s*>\s*)?'
    r'<b>([+-])\s*(\d+)</b>',
    re.DOTALL
)

# Skill name: inside skillgrad-font div → first <span>NAME</span>
NAME_RE = re.compile(r'skillgrad-font.*?<span[^>]*>(.*?)</span>', re.DOTALL)

# Sin affinity: from skill icon frame overlay image alt text
# Pattern: alt="Wrath1.png", alt="Pride2.png", alt="Gloom3BG.png"
SIN_FRAME_RE = re.compile(
    r'alt="(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)\d+(?:BG)?\.png"',
    re.I
)

# Fallback: passive sin icons use alt="LcbSinPride.png"
SIN_LCB_RE = re.compile(
    r'alt="LcbSin(Wrath|Lust|Sloth|Gluttony|Gloom|Pride|Envy)\.png"',
    re.I
)



# ══════════════════════════════════════════════════════════════════════════════
# SPECIAL-CASED IDENTITIES
# ══════════════════════════════════════════════════════════════════════════════
# Hard-coded data for identities that can't be parsed from text alone.
# These won't change until a rework or Uptie V.


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
    """N Corp Yi Sang's S3-2 (Ryoshu's S4: I Shall Fire / Anytime).

    This skill replaces Yi Sang's S3 when Ryoshu's passive triggers.
    Hard-coded from N Corp. E.G.O::Contempt, Awe Ryoshu's 'I Shall Fire / Anytime'.
    """
    return Skill(
        label="S3-2", name="I Shall Fire / Anytime",
        base_power=4, coin_value=7, num_coins=2,
        damage_type="Pierce", sin_affinity="Pride",
        offense_level_offset=5, atk_weight=2, deck_count=0,
        has_unbreakable_coins=True,
        base_bonuses=SkillBonuses(),
        best_case=SkillBonuses(
            base_power_add=2,
            coin_power_add=2,
            atk_weight_add=2,
            notes=[
                "Base Power +2 max (per 3 Poise Count)",
                "Coin Power +2 max (per 5 Poise)",
                "Atk Weight +2 max (per 3 Torn Memory)",
            ],
        ),
        coin_effects=[
            CoinEffect(coin=1, is_unbreakable=True, is_conditional=False,
                        notes=["Unbreakable Coin", "Deals damage only against main target"]),
            CoinEffect(coin=2, dmg_bonus=380, dmg_bonus_on_crit=120,
                        is_unbreakable=True, is_conditional=True,
                        notes=[
                            "Unbreakable Coin",
                            "Deal +40% per Torn Memory (max 280%)",
                            "+5% Crit per Poise Potency (max 50%)",
                            "+10% Crit per Torn Memory (max 70%)",
                            "Deal +1% per 1% missing HP (max 100%)",
                        ]),
        ],
        raw_conditionals=[
            "Gain +1 Atk Weight for every 3 Torn Memory on self (max 2)",
            "Coin Power +1 for every 5 Poise on self (max 2)",
            "Base Power +1 for every 3 Poise Count on self (max 2)",
            "Gain 1 Poise for every Torn Memory on self (max 7)",
        ],
    )


# Map of identity names to builder functions
SPECIAL_CASE_IDENTITIES = {
    "Firefist Office Survivor Gregor": _build_firefist_gregor,
}

# N Corp Yi Sang's title for post-processing
_NCORP_YISANG_TITLE = "N Corp. E.G.O::Fell Bullet Yi Sang"


def parse_identity(raw_html: str, title: str) -> Identity:
    """Parse a full identity page HTML into an Identity object."""
    html = clean_html(raw_html)
    sinner = identify_sinner(title)
    identity = Identity(name=title, sinner=sinner)

    # Special-cased identities bypass HTML parsing entirely
    if title in SPECIAL_CASE_IDENTITIES:
        return SPECIAL_CASE_IDENTITIES[title]()

    # Find all skill stat blocks
    stat_matches = list(STAT_LINE_RE.finditer(html))
    if not stat_matches:
        identity.flags.append("NO_SKILL_DATA")
        return identity

    # Find all skill name positions
    name_matches = list(NAME_RE.finditer(html))
    name_positions = [
        (m.start(), re.sub(r'<[^>]+>', '', m.group(1)).strip())
        for m in name_matches
    ]

    # Parse each skill block
    skills: list[Skill] = []
    for idx, match in enumerate(stat_matches):
        pos = match.start()
        next_pos = stat_matches[idx + 1].start() if idx + 1 < len(stat_matches) else len(html)
        # Cap block size to prevent runaway
        next_pos = min(next_pos, pos + 15000)
        block = html[pos:next_pos]
        # Prefix: area BEFORE this stat line (for sin affinity icons which precede the stat)
        prev_start = stat_matches[idx - 1].end() if idx > 0 else max(0, pos - 5000)
        prefix = html[prev_start:pos]

        skill = _parse_skill_block(block, match, name_positions, pos, prefix)
        if skill:
            skills.append(skill)

    # Assign tab labels
    _assign_tab_labels(html, skills, stat_matches)

    # Deduplicate labels: if two skills share a label, the deck_count=0 one is the alt form
    _dedup_labels(skills)

    # Separate defense from attack skills
    attack_skills, defense, defense2 = _separate_defense(skills)

    identity.skills = attack_skills
    identity.defense = defense
    identity.defense2 = defense2

    # Detect flags
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

    # Determine damage type from image alt text
    # Attack types: "Slash.png", "Blunt.png", "Pierce.png"
    # Defense types: "Evade.png", "Guard.png", "Counter.png" — these are NOT damage types
    ATTACK_TYPES = {"Slash.png": "Slash", "Blunt.png": "Blunt", "Pierce.png": "Pierce"}
    DEFENSE_IMGS = {"Evade.png", "Guard.png", "Counter.png", "Charge.png",
                    "Clashable Counter.png", "Clashable Guard.png"}
    if damage_type_raw in ATTACK_TYPES:
        damage_type = ATTACK_TYPES[damage_type_raw]
    elif damage_type_raw in DEFENSE_IMGS:
        damage_type = ""  # Defense skill, no attack damage type
    elif damage_type_raw:
        damage_type = damage_type_raw.replace('.png', '')  # Unknown image, use as-is
    else:
        damage_type = ""  # No image at all
    coin_value = coin_value_abs if coin_sign == '+' else -coin_value_abs
    is_minus = coin_sign == '-'
    is_defense_img = damage_type_raw in DEFENSE_IMGS if damage_type_raw else False

    # Coin count
    coin_imgs = re.findall(r'alt="(Coin(?:\s*-\s*Unbreakable)?\.png)"', block[:3000])
    num_coins = len(coin_imgs)
    if num_coins == 0:
        num_coins = 1  # Fallback

    has_unbreakable = any('Unbreakable' in c for c in coin_imgs)

    # Skill name (appears after stat line in HTML)
    name = ""
    for npos, nname in name_positions:
        if npos > block_global_pos and npos < block_global_pos + len(block):
            name = nname
            break

    # Strip tags from the header area for pattern matching
    header_text = strip_tags(block[:3000])

    # Offense level: "XX (60+Y)" or "XX (60-Y)"
    off_match = re.search(r'(\d+)\s*\(60([+-]\d+)\)', header_text)
    offense_offset = int(off_match.group(2)) if off_match else 0

    # Attack weight: count ⯀ symbols in stripped text
    atk_weight = header_text[:300].count('\u2BC0')  # ⯀
    if atk_weight == 0:
        atk_weight = 1

    # Deck count: "Amt. xN"
    amt_match = re.search(r'Amt\.\s*x(\d+)', header_text)
    deck_count = int(amt_match.group(1)) if amt_match else -1  # -1 = not found (defense)

    # Sin affinity: from skill icon frame images (e.g., alt="Wrath1.png")
    # The frame images appear BEFORE the stat line in the HTML (in the prefix)
    # Search the prefix (area before this stat line) for the LAST sin icon match
    sin = ""
    if prefix:
        # Find the LAST match in the prefix (closest to this skill)
        all_sin = list(SIN_FRAME_RE.finditer(prefix))
        if all_sin:
            sin = all_sin[-1].group(1).capitalize()
    # Fallback: search in the block itself
    if not sin:
        sin_m = SIN_FRAME_RE.search(block[:2000])
        if sin_m:
            sin = sin_m.group(1).capitalize()

    # Parse conditionals from stripped text (before coin effects)
    first_coin_effect = re.search(r'alt="CoinEffect\d+\.png"', block)
    desc_end = first_coin_effect.start() if first_coin_effect else len(block)
    desc_text = strip_tags(block[:desc_end])
    base_bonuses, best_case, raw_conditionals = parse_skill_conditionals(desc_text)

    # Parse per-coin effects
    coin_effects = _parse_all_coin_effects(block, num_coins)

    # Post-process: accumulate "final coin" bonuses onto the actual last coin
    _resolve_final_coin_bonuses(coin_effects, num_coins)

    # Pre-label defense skills if identified by image
    pre_label = "Def" if is_defense_img else ""

    return Skill(
        label=pre_label,  # May be overridden by _assign_tab_labels
        name=name or "Unknown",
        base_power=base_power,
        coin_value=coin_value,
        num_coins=num_coins,
        damage_type=damage_type,
        sin_affinity=sin,
        offense_level_offset=offense_offset,
        atk_weight=atk_weight,
        deck_count=max(0, deck_count),
        is_minus_coin=is_minus,
        has_unbreakable_coins=has_unbreakable,
        base_bonuses=base_bonuses,
        best_case=best_case,
        coin_effects=coin_effects,
        raw_conditionals=raw_conditionals,
    )


def _resolve_final_coin_bonuses(coin_effects: list[CoinEffect], num_coins: int):
    """Accumulate 'final coin deals +X%' bonuses from all coins onto the actual last coin."""
    total_final_bonus = 0
    for ce in coin_effects:
        if ce.final_coin_dmg_bonus:
            total_final_bonus += ce.final_coin_dmg_bonus
            ce.final_coin_dmg_bonus = 0  # Clear from source coin

    if total_final_bonus == 0:
        return

    # Find or create the last coin's effect entry
    last_ce = next((ce for ce in coin_effects if ce.coin == num_coins), None)
    if last_ce:
        last_ce.dmg_bonus += total_final_bonus
    else:
        coin_effects.append(CoinEffect(coin=num_coins, dmg_bonus=total_final_bonus))


def _parse_all_coin_effects(block: str, num_coins: int) -> list[CoinEffect]:
    """Find all CoinEffect markers and parse each section."""
    effects = []
    markers = list(re.finditer(r'alt="CoinEffect(\d+)\.png"', block))

    for i, marker in enumerate(markers):
        coin_num = int(marker.group(1))
        start = marker.start()
        end = markers[i + 1].start() if i + 1 < len(markers) else min(len(block), start + 1500)
        section = strip_tags(block[start:end])
        ce = parse_coin_effects(section, coin_num)
        effects.append(ce)

    return effects


# ══════════════════════════════════════════════════════════════════════════════
# TAB LABEL ASSIGNMENT
# ══════════════════════════════════════════════════════════════════════════════

def _assign_tab_labels(html: str, skills: list[Skill], stat_matches: list[re.Match]):
    """Assign S1/S2/S3/Def labels based on tab headers in the HTML."""

    # Try EGO-style sub-tabs first: "Skill 1 - 1", "Skill 1 - 2"
    ego_tabs = [(m.start(), f"S{m.group(1)}-{m.group(2)}")
                for m in re.finditer(r'>?\s*Skill\s+(\d+)\s*-\s*(\d+)\s*<?', html)]

    # Simple tabs: "Skill 1", "Skill 2", "Skill 3"
    simple_tabs = [(m.start(), f"S{m.group(1)}")
                   for m in re.finditer(r'>?\s*Skill\s+(\d+)\s*(?!-)', html)]

    # Prefer ego-style sub-tabs (more specific) when available
    content_tabs = ego_tabs if ego_tabs else simple_tabs

    # Also look for "Defense" tab
    for m in re.finditer(r'>?\s*Defense\s*<?', html):
        content_tabs.append((m.start(), "Def"))

    if not content_tabs:
        # Fallback: assign by Amt. value and position
        _assign_labels_by_amt(skills)
        return

    # Group consecutive tab headers (they cluster in the HTML)
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

    # Match skills to tab groups by position
    skill_positions = [(stat_matches[i].start(), i) for i in range(len(skills))]
    for group in tab_groups:
        group_end = group[-1][0]
        gidx = tab_groups.index(group)
        next_start = tab_groups[gidx + 1][0][0] if gidx + 1 < len(tab_groups) else float('inf')

        following = [(p, si) for p, si in skill_positions if group_end < p < next_start]
        for j, (_, si) in enumerate(following):
            if j < len(group):
                skills[si].label = group[j][1]

    # Label any remaining unlabeled skills
    _assign_labels_by_amt([s for s in skills if not s.label])
    for s in skills:
        if not s.label:
            s.label = "Unknown"


def _dedup_labels(skills: list[Skill]):
    """When multiple skills share a label, rename to SX-1 / SX-2 format."""
    from collections import defaultdict
    by_label: dict[str, list[Skill]] = defaultdict(list)
    for s in skills:
        if s.label:
            by_label[s.label].append(s)

    for label, group in by_label.items():
        if len(group) <= 1:
            continue
        # The one with deck_count > 0 gets "-1"; the one with deck_count=0 gets "-2"
        for s in group:
            if s.deck_count > 0:
                s.label = f"{label}-1"
            else:
                s.label = f"{label}-2"


def _assign_labels_by_amt(skills: list[Skill]):
    """Fallback: assign labels based on Amt. values."""
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
# ══════════════════════════════════════════════════════════════════════════════

def _separate_defense(
    skills: list[Skill],
) -> tuple[list[Skill], Defense | None, Defense | None]:
    """Separate defense skill(s) from attack skills."""
    attack = []
    defenses: list[Defense] = []

    for s in skills:
        is_def = False

        # Check label (from image detection or tab assignment)
        if s.label.startswith("Def"):
            is_def = True

        # Override: if it has a damage type AND deck_count > 0, it's NOT defense
        # (tab assignment sometimes mislabels S3 as Def due to proximity)
        if is_def and s.damage_type and s.deck_count > 0:
            is_def = False
            s.label = ""  # Clear mislabel, will be fixed by _assign_labels_by_amt below

        # Check: no damage type = likely defense
        if not s.damage_type and not is_def:
            is_def = True

        # Check name keywords (only if no damage type and no deck count)
        if s.name and not is_def and not s.damage_type:
            lower = s.name.lower()
            if any(k in lower for k in ['evade', 'guard', 'counter', 'charge', 'block', 'dodge', 'parry']):
                is_def = True

        if is_def:
            dtype = _detect_defense_type(s)
            defenses.append(Defense(
                name=s.name,
                defense_type=dtype,
                base_power=s.base_power,
                coin_value=s.coin_value,
                num_coins=s.num_coins,
                offense_level_offset=s.offense_level_offset,
            ))
        else:
            attack.append(s)

    # Fix any attack skills that had their label cleared by the override
    _assign_labels_by_amt(attack)

    def1 = defenses[0] if len(defenses) >= 1 else None
    def2 = defenses[1] if len(defenses) >= 2 else None
    return attack, def1, def2


def _detect_defense_type(skill: Skill) -> str:
    """Detect defense type from skill properties."""
    # Check raw conditionals for clashable markers
    all_text = ' '.join(skill.raw_conditionals).lower()
    if 'clashable counter' in all_text:
        return "Clashable Counter"
    if 'clashable guard' in all_text:
        return "Clashable Guard"

    name_lower = (skill.name or "").lower()
    if 'evade' in name_lower or 'dodge' in name_lower:
        return "Evade"
    if 'guard' in name_lower or 'block' in name_lower:
        return "Guard"
    if 'counter' in name_lower:
        return "Counter"
    if 'charge' in name_lower:
        return "Counter"  # Charge-type defense functions as counter

    # Heuristic: high coin value + low base = Evade, high base = Guard
    if skill.coin_value >= 8:
        return "Evade"
    if skill.base_power >= 10:
        return "Guard"
    return "Counter"


# ══════════════════════════════════════════════════════════════════════════════
# FLAGS
# ══════════════════════════════════════════════════════════════════════════════

def _detect_flags(
    html: str,
    skills: list[Skill],
    defense: Defense | None,
    defense2: Defense | None,
) -> list[str]:
    """Detect mechanical flags for the identity."""
    flags = []

    if any(s.has_unbreakable_coins for s in skills):
        flags.append("HAS_UNBREAKABLE_COINS")
    if any(s.is_minus_coin for s in skills):
        flags.append("HAS_MINUS_COINS")
    if any(s.deck_count == 0 for s in skills):
        flags.append("HAS_CONDITIONAL_ONLY_SKILLS")
    if any(ce.reuse_count > 0 for s in skills for ce in s.coin_effects):
        flags.append("HAS_COIN_REUSE")
    if any(ce.extra_hit_pct > 0 for s in skills for ce in s.coin_effects):
        flags.append("HAS_EXTRA_HIT")
    # Detect alternate skill forms: SX-Y format or Alt label
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
# SINNER IDENTIFICATION
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


def _postprocess_ncorp_link(identities: list[dict]):
    """Add N Corp Yi Sang's S3-2 skill (Ryoshu's I Shall Fire / Anytime)."""
    target = _NCORP_YISANG_TITLE.replace(' - -', '::')
    for identity in identities:
        if identity.get("name", "").replace(' - -', '::') == target:
            s3_2 = _build_ncorp_yisang_s3_2()
            identity["skills"].append(s3_2.to_dict())
            if "HAS_UNBREAKABLE_COINS" not in identity.get("flags", []):
                identity.setdefault("flags", []).append("HAS_UNBREAKABLE_COINS")
            break


def download_all(*, redownload: bool = False, log=None) -> int:
    """Download all identity pages from the wiki into cache.

    Args:
        redownload: If True, fetch everything fresh. If False, skip cached pages.
        log: Optional callable for progress messages (e.g. logger.info).

    Returns:
        Number of pages downloaded (not cached hits).
    """
    _log = log or (lambda msg: print(msg, file=sys.stderr))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    client = CachedClient(CACHE_DIR)

    _log("Fetching identity list from wiki API...")
    titles = list_all_identities()
    _log(f"Found {len(titles)} identities")

    downloaded = 0
    for i, title in enumerate(titles, 1):
        sinner = identify_sinner(title)
        slug = _title_to_slug(title)
        cache_path = CACHE_DIR / sinner / f"{slug}.html"

        if not redownload and cache_path.exists():
            continue

        client._rate_limit()
        html = fetch_page_html(title)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(html, encoding="utf-8")
        downloaded += 1
        _log(f"  [{i:3d}/{len(titles)}] Downloaded: {title}")

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
    _log = log or (lambda msg: print(msg, file=sys.stderr))

    html_files = sorted(CACHE_DIR.rglob("*.html"))
    if not html_files:
        _log(f"No cached pages found in {CACHE_DIR}. Run download_all() first.")
        return []

    _log(f"Parsing {len(html_files)} cached pages...")

    identities: list[dict] = []
    for i, fpath in enumerate(html_files, 1):
        # Title from filename slug → restore spaces for parser
        title = fpath.stem.replace('-', ' ')
        # Try to get a better title from the HTML <title> tag or use the parent dir + slug
        sinner = fpath.parent.name

        try:
            html = fpath.read_text(encoding="utf-8")

            # Extract actual title from HTML if available
            title_match = re.search(r'<title>(.+?)(?:\s*-\s*Limbus Company Wiki)?</title>', html)
            if title_match:
                title = title_match.group(1).strip()

            identity = parse_identity(html, title)
            identities.append(identity.to_dict())

            n_skills = len(identity.skills)
            flags = identity.flags
            status = f"({n_skills} skills)"
            if flags:
                status += f" [{', '.join(flags[:2])}{'...' if len(flags) > 2 else ''}]"
            _log(f"  [{i:3d}/{len(html_files)}] {title} {status}")

        except Exception as e:
            _log(f"  [{i:3d}/{len(html_files)}] FAILED: {title}: {e}")
            identities.append({
                "id": _title_to_slug(title),
                "name": title,
                "sinner": sinner,
                "skills": [],
                "defense": None,
                "flags": ["SCRAPE_FAILED", str(e)[:100]],
            })

    # Post-processing
    _postprocess_ncorp_link(identities)

    # Write output
    output = {
        "version": 3,
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
