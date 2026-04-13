/**
 * engine.js — Pure damage calculation engine for Limbus Company.
 * No DOM, no state — just math. All functions are pure.
 *
 * Skill model (flat, pre-baked):
 *   base_power, coin_value, num_coins, is_minus_coin,
 *   offense_level_offset, skill_dmg_bonus, atk_weight,
 *   coins: [{ coinValueOverride, dmgBonusAdd, extraHitPct }]
 *
 * Settings model:
 *   enemyDefLevel: number (default 60)
 *   physResist: number (multiplier, e.g. 1.0 = Normal, 2.0 = Fatal, 0.5 = Ineff)
 *   sinResist: number (same scale)
 *
 * Buff model:
 *   basePowerAdd, coinPowerAdd, dmgAdd, offLevelAdd
 *
 * Crit is always assumed. The +20% base crit bonus is always in the pool.
 */

export const num = (v, fb = 0) => { const x = parseFloat(v); return isNaN(x) ? fb : x; };

/**
 * Offense/Defense level modifier (diminishing returns curve).
 */
export function offDefMod(off, def) {
  const diff = off - def;
  if (diff === 0) return 0;
  return (diff / (Math.abs(diff) + 25)) * 100;
}

/**
 * Convert a resistance multiplier (e.g. 2.0x) to the additive pool percentage.
 * 1.0x → 0%, 2.0x → +100%, 0.5x → -50%
 */
export function resistToPool(multiplier) {
  return (multiplier - 1) * 100;
}

/**
 * Combine multiple buffs into a single additive buff.
 */
export function combineBuffs(buffs) {
  let base = 0, coin = 0, dmg = 0, offLevel = 0;
  const names = [];
  for (const b of buffs) {
    base += b.basePowerAdd || 0;
    coin += b.coinPowerAdd || 0;
    dmg += b.dmgAdd || 0;
    offLevel += b.offLevelAdd || 0;
    if (b.name) names.push(b.name);
  }
  return { basePowerAdd: base, coinPowerAdd: coin, dmgAdd: dmg, offLevelAdd: offLevel, label: names.join(' + ') };
}

/**
 * Check if a combined buff has any non-zero values.
 */
export function hasBuffValues(buff) {
  return !!(buff.basePowerAdd || buff.coinPowerAdd || buff.dmgAdd || buff.offLevelAdd);
}

/**
 * Calculate damage for a flat skill model (pre-baked base/enhanced values).
 *
 * @param {object} skill - Flat skill with per-coin overrides
 * @param {object|null} buff - Combined external buff (or null)
 * @param {object} settings - { enemyDefLevel, physResist, sinResist }
 * @returns {{ rows: Array, total: number }}
 */
export function calcDamage(skill, buff, settings) {
  const basePower = skill.base_power + (buff ? buff.basePowerAdd : 0);
  const globalCoin = skill.coin_value + (buff ? buff.coinPowerAdd : 0);

  const offLevel = (settings.idLevel || 60) + (skill.offense_level_offset || 0) + (buff ? (buff.offLevelAdd || 0) : 0);
  const offDef = offDefMod(offLevel, settings.enemyDefLevel || 60);
  const physR = resistToPool(settings.physResist ?? 1.0);
  const sinR = resistToPool(settings.sinResist ?? 1.0);

  const basePool = offDef + physR + sinR
    + (skill.skill_dmg_bonus || 0)
    + (buff ? buff.dmgAdd : 0);

  const rows = [];
  let cum = basePower;

  for (let i = 0; i < skill.num_coins; i++) {
    const c = skill.coins?.[i] || {};

    // Coin value: use override if set, otherwise global
    const coinVal = (c.coinValueOverride !== '' && c.coinValueOverride != null)
      ? num(c.coinValueOverride) + (buff ? buff.coinPowerAdd : 0)
      : globalCoin;

    const prevCum = cum;
    if (coinVal >= 0) {
      cum += coinVal;
    }

    const raw = Math.max(cum > 0 ? 1 : 0, cum);
    const pool = basePool + num(c.dmgBonusAdd);
    const mult = 1 + pool / 100;
    const dmg = Math.max(raw > 0 ? 1 : 0, Math.floor(raw * mult));

    const extraHit = num(c.extraHitPct) ? Math.floor(dmg * num(c.extraHitPct) / 100) : 0;
    const coinTotal = dmg + extraHit;

    rows.push({
      n: i + 1,
      coinVal,
      prevRaw: prevCum,
      raw: cum,
      pool: Math.round(pool * 10) / 10,
      mult: Math.round(mult * 1000) / 1000,
      dmg,
      coinTotal,
    });
  }

  return { rows, total: rows.reduce((s, r) => s + r.coinTotal, 0) };
}

/**
 * Format a number for display.
 */
export function fmt(v) {
  if (v === null || v === undefined) return '—';
  if (v % 1 === 0) return String(v);
  return v.toFixed(1);
}
