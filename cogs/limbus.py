"""Cog for Limbus Company damage calculator features.

This cog provides:
- NLP handler for rolling Limbus Company skills. Supports:
  * Named skill lookup: "limbus roll Yi Sang S1" → uses real identity data
  * Manual parameters: "limbus 4 base 3 cp 2 coins 10 sp" → old-style parsing
  * Interactive fallback when parameters are missing
- Admin scrape command to refresh identity data from the wiki
- Dashboard for editing/correcting parsed skill data (Components V2)

Data is loaded from assets/identities.json on cog_ready().
The web calculator is served by the Web cog at /limbus.
"""

import asyncio
import json
import os
import random
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

import config
from utils.base_cog import BaseCog

if TYPE_CHECKING:
    from utils.bot_class import CoreBot


class Limbus(BaseCog):
    """Limbus Company skill data, rolling, and management."""

    def __init__(self, bot: "CoreBot") -> None:
        super().__init__(bot)
        self._identities: List[Dict[str, Any]] = []
        self._by_sinner: Dict[str, List[Dict[str, Any]]] = {}
        self._scraped_at: Optional[str] = None
        self._loaded = False

    async def cog_ready(self) -> None:
        """Load identity data from assets/identities.json."""
        self._load_data()

    def _load_data(self) -> None:
        """Load and index identities.json from disk."""
        path = os.path.join(config.ASSETS_PATH, "identities.json")
        if not os.path.exists(path):
            self.logger.warning(f"Identity data not found at {path}")
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._identities = data.get("identities", [])
            self._scraped_at = data.get("scraped_at")
            self._by_sinner.clear()
            for identity in self._identities:
                sinner = identity.get("sinner", "Unknown")
                self._by_sinner.setdefault(sinner, []).append(identity)

            self._loaded = True
            self.logger.info(
                f"Loaded {len(self._identities)} identities "
                f"({len(self._by_sinner)} sinners)"
            )
        except Exception as e:
            self.logger.error(f"Failed to load identity data: {e}", exc_info=True)

    # ========== NLP HANDLERS ==========

    async def limbus_roll_nlp(self, ctx: commands.Context, *, query: str) -> None:
        """Roll a Limbus Company skill — either by name or by manual parameters.

        First attempts to find a named identity/skill in the query.
        If no match, falls back to the old sequential parameter parsing
        (base power, coin power, coin count, SP) with interactive fallback.

        Args:
            ctx: The command context.
            query: The user's input string.
        """
        # Try named skill lookup first
        result = self._try_named_roll(query)
        if result:
            identity, skill, roll = result
            await ctx.send(self._format_named_roll(ctx.author, identity, skill, roll))
            return

        # Fall back to manual parameter parsing
        await self._manual_roll(ctx, query)

    # ── Named skill roll ──

    def _try_named_roll(self, query: str) -> Optional[tuple]:
        """Attempt to match a named identity/skill in the query.

        Returns:
            (identity_dict, skill_dict, roll_result) if found, else None.
        """
        if not self._loaded:
            return None

        # Strip trigger words
        clean = re.sub(r'\blimbus\b', '', query, flags=re.IGNORECASE).strip()
        clean = re.sub(r'^(roll|flip|use)\s+', '', clean, flags=re.IGNORECASE).strip()

        if not clean:
            return None

        # Extract skill label (S1, S2, S3, S1-alt, etc.)
        skill_label_match = re.search(r'\b(S[1-3](?:-alt)?)\b', clean, re.IGNORECASE)
        skill_label = skill_label_match.group(1).upper() if skill_label_match else None

        # Extract the name portion
        if skill_label_match:
            name_query = (
                clean[:skill_label_match.start()].strip()
                + " "
                + clean[skill_label_match.end():].strip()
            ).strip()
        else:
            name_query = clean

        if not name_query:
            return None

        # Search for matching identities
        matches = self._search_identities(name_query)
        if not matches:
            return None

        identity = matches[0]
        skills = identity.get("skills", [])
        if not skills:
            return None

        # Find the specific skill
        if skill_label:
            skill = next((s for s in skills if s["label"].upper() == skill_label), None)
            if not skill:
                return None
        else:
            skill = skills[0]  # Default to first skill (S1)

        roll = self._roll_skill(skill)
        return (identity, skill, roll)

    def _search_identities(self, query: str) -> List[Dict[str, Any]]:
        """Search identities by name (case-insensitive substring match)."""
        q = query.lower()
        scored = []
        for identity in self._identities:
            name = identity.get("name", "").lower()
            sinner = identity.get("sinner", "").lower()

            if q == name:
                scored.append((0, identity))  # Exact match
            elif q in name:
                scored.append((1, identity))  # Substring of identity name
            elif q in sinner:
                scored.append((2, identity))  # Matches sinner name
            elif any(word in name for word in q.split()):
                scored.append((3, identity))  # Partial word match

        scored.sort(key=lambda x: x[0])
        return [item[1] for item in scored]

    def _roll_skill(self, skill: Dict[str, Any]) -> Dict[str, Any]:
        """Roll a skill: apply base_bonuses, flip coins, compute power.

        Uses the non-max conditional (base_bonuses) version.

        Returns:
            Dict with keys: base_power, coin_value, num_coins, coins,
            heads_count, coin_total, final_power, is_minus
        """
        base_power = skill.get("base_power", 0)
        coin_value = skill.get("coin_value", 0)
        num_coins = skill.get("num_coins", 1)
        is_minus = skill.get("is_minus_coin", False)

        # Apply base_bonuses (unconditional bonuses)
        bonuses = skill.get("base_bonuses", {})
        base_power += bonuses.get("base_power_add", 0)
        coin_value += bonuses.get("coin_power_add", 0)

        # Flip coins (50% heads probability)
        coins = []
        heads_count = 0
        for _ in range(num_coins):
            if random.random() < 0.5:
                heads_count += 1
                coins.append("H")
            else:
                coins.append("T")

        # For minus coins: tails ADD coin_value (power decreases on heads)
        # For plus coins: heads ADD coin_value
        if is_minus:
            tails_count = num_coins - heads_count
            coin_total = tails_count * coin_value
        else:
            coin_total = heads_count * coin_value

        final_power = base_power + coin_total

        return {
            "base_power": base_power,
            "coin_value": coin_value,
            "num_coins": num_coins,
            "is_minus": is_minus,
            "coins": coins,
            "heads_count": heads_count,
            "coin_total": coin_total,
            "final_power": final_power,
        }

    def _format_named_roll(
        self,
        author: discord.Member | discord.User,
        identity: Dict[str, Any],
        skill: Dict[str, Any],
        result: Dict[str, Any],
    ) -> str:
        """Format a named skill roll result into a Discord message."""
        coin_str = " ".join(result["coins"])
        h = result["heads_count"]
        t = result["num_coins"] - h
        cv_str = f"+{result['coin_value']}" if result['coin_value'] >= 0 else str(result['coin_value'])

        return (
            f"**{identity['name']}** — {skill['label']}: {skill['name']}\n"
            f"{skill.get('damage_type', '?')} · {skill.get('sin_affinity', '?')}\n"
            f"Flipping {result['num_coins']} coins (value: {cv_str}): `{coin_str}`\n"
            f"Result: {h}H {t}T → **{result['coin_total']:+d}**\n"
            f"{author.mention}, total power: "
            f"`{result['base_power']} + ({result['coin_total']:+d})` = **{result['final_power']}**"
        )

    # ── Manual parameter roll (old-style) ──

    async def _manual_roll(self, ctx: commands.Context, query: str) -> None:
        """Parse manual parameters from the query and roll.

        Supports: "4 base 3 cp 2 coins 10 sp"
        Falls back to interactive prompts for missing parameters.
        """
        def check(m: discord.Message) -> bool:
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            # Pad query for reliable regex matching.
            # Each parameter is searched for, extracted, and removed to prevent re-parsing.
            work_query = f" {query.lower()} "
            base_power, coin_power, num_coins, sp = None, None, None, None

            sp_match = re.search(r'(?:at\s+)?(-?\d+)\s+sp\b', work_query, re.IGNORECASE)
            if sp_match:
                sp = int(sp_match.group(1))
                work_query = work_query.replace(sp_match.group(0), " ", 1)

            base_match = re.search(
                r'(?:(\d+)\s+\b(base\s*power|bp)\b|\b(base\s*power|bp)\b\s+(\d+))',
                work_query, re.IGNORECASE,
            )
            if base_match:
                base_power = int(base_match.group(1) or base_match.group(4))
                work_query = work_query.replace(base_match.group(0), " ", 1)

            cp_match = re.search(
                r'(?:([+-]?\d+)\s+\b(coin\s*power|cp)\b|\b(coin\s*power|cp)\b\s+([+-]?\d+))',
                work_query, re.IGNORECASE,
            )
            if cp_match:
                coin_power = int(cp_match.group(1) or cp_match.group(4))
                work_query = work_query.replace(cp_match.group(0), " ", 1)

            num_match = re.search(
                r'(?:(\d+)\s+\b(coins?|coin\s*count)\b|\b(coins?|coin\s*count)\b\s+(\d+))',
                work_query, re.IGNORECASE,
            )
            if num_match:
                num_coins = int(num_match.group(1) or num_match.group(4))
                work_query = work_query.replace(num_match.group(0), " ", 1)

            # Extract remaining signed number as modifier
            mod_match = re.search(r'\s([+-]\d+)\s', work_query)
            modifier = int(mod_match.group(1)) if mod_match else 0

            # Fallback to interactive mode if parameters missing
            interactive_fallback_needed = any(
                v is None for v in [base_power, coin_power, num_coins, sp]
            )
            if interactive_fallback_needed:
                await ctx.send(
                    "Switching to interactive mode, please input your values below.\n"
                    "*If you provided all the info, please let my author know something is broken!*"
                )

            if base_power is None:
                await ctx.send("Base power?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                base_power = int(msg.content)

            if coin_power is None:
                await ctx.send("Coin power?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                coin_power = int(msg.content)

            if num_coins is None:
                await ctx.send("How many coins?")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                num_coins = int(msg.content)

            if sp is None:
                await ctx.send("SP? (optional, press enter to skip)")
                msg = await self.bot.wait_for('message', check=check, timeout=30.0)
                sp = int(msg.content.strip()) if msg.content.strip() else 0

            # Validation
            if not (1 <= num_coins <= 15):
                raise ValueError("Coin count must be between 1 and 15.")
            if not (-50 <= coin_power <= 50):
                raise ValueError("Coin value must be between -50 and 50.")
            if not (-100 <= base_power <= 100) or not (-100 <= modifier <= 100):
                raise ValueError("Base power and modifiers must be between -100 and 100.")
            if not (-45 <= sp <= 45):
                raise ValueError("SP must be between -45 and 45.")

            # Simulate coin flips
            heads_prob = 0.5 + (0.01 * sp)
            heads_count = 0
            coin_results_display = []
            for _ in range(num_coins):
                if random.random() < heads_prob:
                    heads_count += 1
                    coin_results_display.append("H")
                else:
                    coin_results_display.append("T")

            coin_total = heads_count * coin_power
            final_result = base_power + coin_total + modifier

            # Format and send
            coin_part_str = f"{heads_count}H {len(coin_results_display) - heads_count}T"
            coin_value_str = f"+{coin_power}" if coin_power >= 0 else str(coin_power)
            sp_info = f" at **{sp} SP** (Heads Chance: **{heads_prob:.0%}**)" if sp != 0 else ""

            description = (
                f"Flipping {num_coins} coins{sp_info} (Value: {coin_value_str}): "
                f"`{' '.join(coin_results_display)}`\n"
                f"Result: {coin_part_str} -> **{coin_total}**"
            )

            response = (
                f"{ctx.author.mention}, your roll result is: **{final_result}**\n"
                f"Calculation: `(Base) {base_power} + (Coins) {coin_total} + (Mods) {modifier}`\n"
                f"{description}"
            )
            await ctx.send(response)

        except asyncio.TimeoutError:
            await ctx.send("You took too long to answer, so I cancelled the roll.")
        except (ValueError, TypeError) as e:
            await ctx.send(f"Invalid input: {e}. Please enter a valid number.")
        except Exception as e:
            await ctx.send(f"An unexpected error occurred: {e}")
            self.logger.error(f"Error during limbus roll for {ctx.author}: {e}", exc_info=True)

    # ========== DATA MANAGEMENT ==========

    def _save_data(self) -> None:
        """Write the current identity data back to assets/identities.json.

        Uses write-to-temp-then-rename to avoid corrupting the file on partial writes.
        """
        path = os.path.join(config.ASSETS_PATH, "identities.json")
        tmp_path = path + ".tmp"
        data = {
            "version": 3,
            "scraped_at": self._scraped_at or datetime.now(timezone.utc).isoformat(),
            "identity_count": len(self._identities),
            "identities": self._identities,
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
            self.logger.info(f"Saved {len(self._identities)} identities to {path}")
        except Exception as e:
            self.logger.error(f"Failed to save identity data: {e}", exc_info=True)
            # Clean up temp file if it exists
            try:
                os.remove(tmp_path)
            except OSError:
                pass


    # ── Identity lookup ──

    def get_identity(self, identity_id: str) -> Optional[Dict[str, Any]]:
        """Get an identity by its ID slug."""
        for identity in self._identities:
            if identity.get("id") == identity_id:
                return identity
        return None

    # ── Skill editing ──

    def update_skill(self, identity_id: str, skill_label: str, updates: Dict[str, Any]) -> bool:
        """Update fields on a specific skill and mark it as manually edited.

        For nested dicts (base_bonuses, best_case), merges instead of
        overwriting to preserve the notes list.

        Args:
            identity_id: The identity ID slug.
            skill_label: The skill label (S1, S2, S3, etc.).
            updates: Dict of field_name -> new_value to apply.

        Returns:
            True if the skill was found and updated, False otherwise.
        """
        identity = self.get_identity(identity_id)
        if not identity:
            return False

        for skill in identity.get("skills", []):
            if skill["label"] == skill_label:
                for key, value in updates.items():
                    if isinstance(value, dict) and isinstance(skill.get(key), dict):
                        skill[key].update(value)
                    else:
                        skill[key] = value
                skill["manually_edited"] = True
                self._save_data()
                return True
        return False

    # ── Rescrape pipeline ──

    def _extract_manual_overrides(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Extract manually-edited skills from current data for preservation.

        Returns:
            Nested dict: {identity_id: {skill_label: skill_dict}}.
        """
        overrides: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for identity in self._identities:
            identity_id = identity.get("id", "")
            for skill in identity.get("skills", []):
                if skill.get("manually_edited"):
                    overrides.setdefault(identity_id, {})[skill["label"]] = skill
        return overrides

    def _apply_manual_overrides(self, overrides: Dict[str, Dict[str, Dict[str, Any]]]) -> int:
        """Re-apply manually-edited skills after a rescrape.

        Returns:
            Number of skills restored.
        """
        count = 0
        for identity in self._identities:
            identity_id = identity.get("id", "")
            if identity_id not in overrides:
                continue
            id_overrides = overrides[identity_id]
            for i, skill in enumerate(identity.get("skills", [])):
                if skill["label"] in id_overrides:
                    identity["skills"][i] = id_overrides[skill["label"]]
                    count += 1
        if count > 0:
            self._save_data()
        return count

    async def rescrape(
        self,
        redownload: bool,
        log: Callable[[str], None],
    ) -> Tuple[int, int, int]:
        """Run the rescrape pipeline: download (optional) -> parse -> reload.

        Preserves any skills marked as manually_edited.

        Args:
            redownload: If True, re-download all pages from wiki first.
            log: Callable for progress messages.

        Returns:
            (pages_downloaded, identities_parsed, manual_overrides_preserved)
        """
        from utils.limbus_wiki import download_all, parse_all

        # 1. Save manual overrides before the file gets overwritten
        overrides = self._extract_manual_overrides()
        override_count = sum(len(v) for v in overrides.values())
        if override_count:
            log(f"Preserved {override_count} manually-edited skill(s)")

        # 2. Download (synchronous — run in thread)
        downloaded = 0
        if redownload:
            downloaded = await asyncio.to_thread(download_all, redownload=True, log=log)

        # 3. Parse (synchronous — run in thread, writes identities.json)
        identities = await asyncio.to_thread(parse_all, log=log)

        # 4. Reload the freshly-written file
        self._load_data()

        # 5. Re-apply manual overrides
        restored = 0
        if overrides:
            restored = self._apply_manual_overrides(overrides)
            log(f"Restored {restored} manually-edited skill(s)")

        return (downloaded, len(identities), restored)


async def setup(bot: "CoreBot") -> None:
    """Standard setup function for the cog."""
    await bot.add_cog(Limbus(bot))
