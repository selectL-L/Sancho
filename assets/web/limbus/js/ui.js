/**
 * ui.js — UI rendering, event handling, and state management.
 *
 * React-style skill cards: one input row + per-coin overrides.
 * Base/Enhanced chosen at add-time via picker popup.
 * Damage tables show one tier (+ buffed side-by-side if buffs exist).
 */

import { calcDamage, combineBuffs, hasBuffValues, fmt, num } from './engine.js';
import { loadData, getSinners, getIdentitiesForSinner, getAllIdentities, getIdentityById } from './data.js';

// ── Helpers ──

let _uid = Date.now();
function uid() { return String(++_uid); }

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

const STORAGE_KEY = 'limbus-calc-saved-buffs';
const CUSTOM_SKILLS_KEY = 'limbus-calc-custom-skills';
const RESIST_TIERS = [0.5, 0.75, 1.0, 1.5, 2.0];

function loadSavedBuffs() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); } catch { return []; }
}
function saveBuff(buff) {
  const saved = loadSavedBuffs();
  saved.push({ name: buff.name, basePowerAdd: buff.basePowerAdd, coinPowerAdd: buff.coinPowerAdd, dmgAdd: buff.dmgAdd, offLevelAdd: buff.offLevelAdd });
  localStorage.setItem(STORAGE_KEY, JSON.stringify(saved));
}

function loadCustomSkills() {
  try { return JSON.parse(localStorage.getItem(CUSTOM_SKILLS_KEY) || '[]'); } catch { return []; }
}
function saveCustomSkills(skills) {
  localStorage.setItem(CUSTOM_SKILLS_KEY, JSON.stringify(skills));
}

function findEntry(id) {
  for (const bucket of state.buckets) {
    const entry = bucket.skills.find(s => s.id === id);
    if (entry) return entry;
  }
  return null;
}

function getCombined() { return combineBuffs(state.buffs); }

// ── State ──

const state = {
  loaded: false,
  settings: { idLevel: 60, enemyDefLevel: 60, physResist: 1.0, sinResist: 1.0 },
  buffs: [{ id: uid(), name: '2 Damage Up', basePowerAdd: 0, coinPowerAdd: 0, dmgAdd: 20, offLevelAdd: 0 }],
  buckets: [{ id: uid(), name: 'Compare', skills: [] }],
  selectedSinner: '',
  selectedIdentity: '',
  popup: null,  // { label } when a skill button's Base/Enhanced popup is open
};

// ── Init ──

export async function init() {
  try {
    await loadData();
    state.loaded = true;
    renderAll();
    setupBuffEvents();
    setupBucketEvents();
  } catch (e) {
    document.getElementById('app').innerHTML = `<div class="error">Failed to load data: ${e.message}</div>`;
  }
}

// ── Rendering ──

function renderAll() {
  renderPicker();
  renderBuffBar();
  renderSettings();
  renderBuckets();
}

// ── Bake skill: apply base/enhanced bonuses into flat values ──

function bakeSkill(rawSkill, tier) {
  const bonuses = tier === 'base' ? (rawSkill.base_bonuses || {}) : (rawSkill.best_case || {});
  const baseCoin = rawSkill.coin_value + (bonuses.coin_power_add || 0);

  const coins = [];
  for (let i = 0; i < rawSkill.num_coins; i++) {
    const coinNum = i + 1;
    const effects = (rawSkill.coin_effects || []).filter(ce => ce.coin === coinNum);

    let hasPowerAdd = false;
    let totalPowerAdd = 0;
    let dmgBonus = 0;
    let extraHit = 0;
    for (const ce of effects) {
      const include = tier === 'enhanced' || !ce.is_conditional;
      if (include) {
        if (ce.power_add) { hasPowerAdd = true; totalPowerAdd += ce.power_add; }
        dmgBonus += ce.dmg_bonus || 0;
        extraHit += ce.extra_hit_pct || 0;
      }
      // dmg_bonus_on_crit is ALWAYS included (crit always assumed)
      dmgBonus += ce.dmg_bonus_on_crit || 0;
    }

    coins.push({
      coinValueOverride: hasPowerAdd ? String(baseCoin + totalPowerAdd) : '',
      dmgBonusAdd: dmgBonus ? String(dmgBonus) : '',
      extraHitPct: extraHit ? String(extraHit) : '',
    });
  }

  return {
    label: rawSkill.label,
    name: rawSkill.name,
    base_power: rawSkill.base_power + (bonuses.base_power_add || 0),
    coin_value: baseCoin,
    num_coins: rawSkill.num_coins,
    damage_type: rawSkill.damage_type,
    sin_affinity: rawSkill.sin_affinity || '',
    offense_level_offset: rawSkill.offense_level_offset || 0,
    atk_weight: rawSkill.atk_weight + (bonuses.atk_weight_add || 0),
    skill_dmg_bonus: bonuses.skill_dmg_bonus || 0,
    coins,
  };
}

// ── Picker ──

function renderPicker() {
  const el = document.getElementById('picker');
  const sinners = getSinners();
  const isCustom = state.selectedSinner === '__custom__';
  const customSkills = loadCustomSkills();

  const identities = isCustom ? [] : (state.selectedSinner
    ? getIdentitiesForSinner(state.selectedSinner)
    : getAllIdentities());

  const identity = (!isCustom && state.selectedIdentity) ? getIdentityById(state.selectedIdentity) : null;
  const labels = identity ? [...new Set(identity.skills.map(s => s.label))] : [];
  const hasId = !!identity;

  el.innerHTML = `
    <div class="picker-row">
      <div class="field">
        <label>Sinner</label>
        <select id="sinner-select">
          <option value="">All Sinners</option>
          ${sinners.map(s => `<option value="${s}" ${s === state.selectedSinner ? 'selected' : ''}>${s}</option>`).join('')}
          <option value="__custom__" ${isCustom ? 'selected' : ''}>Custom</option>
        </select>
      </div>
      ${isCustom ? `
        <div class="field">
          <label>Saved Skills</label>
          <select id="custom-skill-select">
            <option value="">Select skill...</option>
            ${customSkills.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('')}
          </select>
        </div>
        <div class="picker-actions" id="picker-actions">
          <button class="btn btn-primary" id="load-custom-btn" disabled>+ Add</button>
          <button class="btn btn-sm" id="delete-custom-btn" disabled>Delete</button>
        </div>
      ` : `
        <div class="field">
          <label>Identity</label>
          <select id="identity-select">
            <option value="">Select identity...</option>
            ${identities.map(i => `<option value="${i.id}" ${i.id === state.selectedIdentity ? 'selected' : ''}>${i.name}</option>`).join('')}
          </select>
        </div>
        <div class="picker-actions" id="picker-actions">
          <button class="btn btn-primary btn-add-skill" data-add-label="__all__" ${hasId ? '' : 'disabled'}>+ All</button>
          ${labels.map(l => `<button class="btn btn-sm btn-add-skill" data-add-label="${esc(l)}" ${hasId ? '' : 'disabled'}>${esc(l)}</button>`).join('')}
          ${state.popup ? renderPopup() : ''}
        </div>
      `}
    </div>`;

  el.querySelector('#sinner-select').addEventListener('change', e => {
    state.selectedSinner = e.target.value;
    state.selectedIdentity = '';
    state.popup = null;
    renderPicker();
  });

  if (isCustom) {
    const sel = el.querySelector('#custom-skill-select');
    const loadBtn = el.querySelector('#load-custom-btn');
    const delBtn = el.querySelector('#delete-custom-btn');
    sel.addEventListener('change', () => {
      const hasVal = sel.value !== '';
      loadBtn.disabled = !hasVal;
      delBtn.disabled = !hasVal;
    });
    loadBtn.addEventListener('click', () => {
      const i = parseInt(sel.value);
      if (isNaN(i)) return;
      const s = customSkills[i];
      const bucket = state.buckets[state.buckets.length - 1];
      bucket.skills.push({
        id: uid(), identityName: 'Custom', sinner: 'Custom', tier: null,
        skill: JSON.parse(JSON.stringify(s)),
      });
      renderBuckets();
    });
    delBtn.addEventListener('click', () => {
      const i = parseInt(sel.value);
      if (isNaN(i)) return;
      customSkills.splice(i, 1);
      saveCustomSkills(customSkills);
      renderPicker();
    });
  } else {
    el.querySelector('#identity-select').addEventListener('change', e => {
      state.selectedIdentity = e.target.value;
      state.popup = null;
      renderPicker();
    });

    for (const btn of el.querySelectorAll('.btn-add-skill')) {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const label = btn.dataset.addLabel;
        if (state.popup && state.popup.label === label) {
          state.popup = null;
        } else {
          state.popup = { label };
        }
        renderPicker();
      });
    }

    const popup = el.querySelector('.tier-popup');
    if (popup) {
      popup.addEventListener('click', e => e.stopPropagation());
      for (const btn of popup.querySelectorAll('.tier-btn')) {
        btn.addEventListener('click', () => {
          addSkills(state.popup.label, btn.dataset.tier);
          state.popup = null;
          renderPicker();
        });
      }
    }

    if (state.popup) {
      const close = () => { state.popup = null; renderPicker(); };
      setTimeout(() => document.addEventListener('click', close, { once: true }), 10);
    }
  }
}

function renderPopup() {
  return `<div class="tier-popup">
    <button class="tier-btn" data-tier="base">Base</button>
    <button class="tier-btn" data-tier="enhanced">Enhanced</button>
  </div>`;
}

function addSkills(label, tier) {
  const identity = getIdentityById(state.selectedIdentity);
  if (!identity) return;
  const bucket = state.buckets[state.buckets.length - 1];
  const skills = label === '__all__'
    ? identity.skills
    : identity.skills.filter(s => s.label === label);

  for (const raw of skills) {
    const baked = bakeSkill(raw, tier);
    bucket.skills.push({
      id: uid(),
      identityName: identity.name,
      sinner: identity.sinner,
      tier,
      skill: baked,
    });
  }
  renderBuckets();
}

function moveSkill(skillId, sourceBucketId, targetBucketId, dropTarget) {
  const src = state.buckets.find(b => b.id === sourceBucketId);
  const tgt = state.buckets.find(b => b.id === targetBucketId);
  if (!src || !tgt) return;
  const idx = src.skills.findIndex(s => s.id === skillId);
  if (idx === -1) return;
  const [skill] = src.skills.splice(idx, 1);
  let insertIdx = tgt.skills.length;
  const card = dropTarget.closest?.('[data-drag-skill]');
  if (card && card.dataset.dragSkill !== skillId) {
    const ti = tgt.skills.findIndex(s => s.id === card.dataset.dragSkill);
    if (ti !== -1) insertIdx = ti + 1;
  }
  tgt.skills.splice(insertIdx, 0, skill);
  renderBuckets();
}

// ── Buff bar — event delegation for focus-safe updates ──

function renderBuffBar() {
  const el = document.getElementById('buff-bar');
  const combined = getCombined();
  const hasBuff = hasBuffValues(combined);
  const saved = loadSavedBuffs();

  el.innerHTML = `
    <div class="section-header">
      <span class="section-title">External Buffs / EGO Gifts</span>
      <div class="btn-group">
        ${saved.length ? `<select id="load-saved-select" class="select-sm">
          <option value="">Load Saved...</option>
          ${saved.map((s, i) => `<option value="${i}">${esc(s.name)}</option>`).join('')}
        </select>` : ''}
        <button id="add-buff-btn" class="btn btn-sm">+ Add Buff</button>
      </div>
    </div>
    <div class="buff-list" id="buff-list">
      ${state.buffs.map(b => `
        <div class="buff-row" data-buff-id="${b.id}">
          <input class="input-sm input-name" data-buff-field="name" value="${esc(b.name || '')}" placeholder="Name">
          <label class="buff-field">+Base <input class="input-sm input-num" data-buff-field="basePowerAdd" type="number" value="${b.basePowerAdd || 0}"></label>
          <label class="buff-field">+Coin <input class="input-sm input-num" data-buff-field="coinPowerAdd" type="number" value="${b.coinPowerAdd || 0}"></label>
          <label class="buff-field">+Dmg% <input class="input-sm input-num" data-buff-field="dmgAdd" type="number" value="${b.dmgAdd || 0}"></label>
          <label class="buff-field">+Off <input class="input-sm input-num" data-buff-field="offLevelAdd" type="number" value="${b.offLevelAdd || 0}"></label>
          <button class="btn-save buff-save-btn" title="Save this buff">Save</button>
          <button class="btn-icon buff-remove" title="Remove">&times;</button>
        </div>
      `).join('')}
    </div>
    <div id="buff-combined" class="${hasBuff ? 'buff-combined' : ''}">${hasBuff ? `Combined: ${combined.basePowerAdd ? `+${combined.basePowerAdd} base` : ''} ${combined.coinPowerAdd ? `+${combined.coinPowerAdd} coin` : ''} ${combined.dmgAdd ? `+${combined.dmgAdd}% dmg` : ''} ${combined.offLevelAdd ? `+${combined.offLevelAdd} off lvl` : ''}` : ''}</div>`;
}

function updateBuffCombined() {
  const el = document.getElementById('buff-combined');
  if (!el) return;
  const combined = getCombined();
  const hasBuff = hasBuffValues(combined);
  el.innerHTML = hasBuff ? `Combined: ${combined.basePowerAdd ? `+${combined.basePowerAdd} base` : ''} ${combined.coinPowerAdd ? `+${combined.coinPowerAdd} coin` : ''} ${combined.dmgAdd ? `+${combined.dmgAdd}% dmg` : ''} ${combined.offLevelAdd ? `+${combined.offLevelAdd} off lvl` : ''}` : '';
  el.className = hasBuff ? 'buff-combined' : '';
}

function setupBuffEvents() {
  const el = document.getElementById('buff-bar');

  // Delegated input handling — no re-render, just update state
  el.addEventListener('input', e => {
    const field = e.target.dataset.buffField;
    const row = e.target.closest('[data-buff-id]');
    if (!field || !row) return;
    const buff = state.buffs.find(b => b.id === row.dataset.buffId);
    if (!buff) return;
    buff[field] = field === 'name' ? e.target.value : (parseFloat(e.target.value) || 0);
    updateBuffCombined();
    renderBuckets();
  });

  // Delegated click handling
  el.addEventListener('click', e => {
    if (e.target.id === 'add-buff-btn') {
      state.buffs.push({ id: uid(), name: '', basePowerAdd: 0, coinPowerAdd: 0, dmgAdd: 0, offLevelAdd: 0 });
      renderBuffBar(); renderBuckets();
      return;
    }
    if (e.target.closest('.buff-remove')) {
      const row = e.target.closest('[data-buff-id]');
      if (row) {
        state.buffs = state.buffs.filter(b => b.id !== row.dataset.buffId);
        renderBuffBar(); renderBuckets();
      }
      return;
    }
    if (e.target.closest('.buff-save-btn')) {
      const row = e.target.closest('[data-buff-id]');
      if (row) {
        const buff = state.buffs.find(b => b.id === row.dataset.buffId);
        if (buff) { saveBuff(buff); renderBuffBar(); }
      }
      return;
    }
  });

  el.addEventListener('change', e => {
    if (e.target.id === 'load-saved-select') {
      const i = parseInt(e.target.value); if (isNaN(i)) return;
      const s = loadSavedBuffs()[i];
      state.buffs.push({ id: uid(), ...s });
      renderBuffBar(); renderBuckets();
    }
  });
}

// ── Settings ──

function renderSettings() {
  const el = document.getElementById('settings');
  const s = state.settings;

  el.innerHTML = `
    <div class="section-header"><span class="section-title">Global Settings</span></div>
    <div class="settings-grid">
      <label>ID Level <input id="s-id" type="number" class="input-sm input-num" value="${s.idLevel}"></label>
      <label>Def Level <input id="s-def" type="number" class="input-sm input-num" value="${s.enemyDefLevel}"></label>
      <div class="field">
        <label>Phys Resist</label>
        <div class="resist-input">
          <input id="s-phys" type="number" class="input-sm input-resist" value="${s.physResist}" step="0.1" min="0" max="4">
          <span class="resist-x">x</span>
          <div class="resist-chips">
            ${RESIST_TIERS.map(v => `<button class="chip ${s.physResist === v ? 'active' : ''}" data-val="${v}">${v}x</button>`).join('')}
          </div>
        </div>
      </div>
      <div class="field">
        <label>Sin Resist</label>
        <div class="resist-input">
          <input id="s-sin" type="number" class="input-sm input-resist" value="${s.sinResist}" step="0.1" min="0" max="4">
          <span class="resist-x">x</span>
          <div class="resist-chips">
            ${RESIST_TIERS.map(v => `<button class="chip ${s.sinResist === v ? 'active' : ''}" data-val="${v}">${v}x</button>`).join('')}
          </div>
        </div>
      </div>
    </div>`;

  el.querySelector('#s-id').addEventListener('change', e => { s.idLevel = parseInt(e.target.value) || 60; renderBuckets(); });
  el.querySelector('#s-def').addEventListener('change', e => { s.enemyDefLevel = parseInt(e.target.value) || 60; renderBuckets(); });
  const bindResist = (inputId, field) => {
    const input = el.querySelector(inputId);
    input.addEventListener('change', e => { s[field] = parseFloat(e.target.value) || 1.0; renderSettings(); renderBuckets(); });
    for (const chip of input.closest('.resist-input').querySelectorAll('.chip')) {
      chip.addEventListener('click', () => { s[field] = parseFloat(chip.dataset.val); renderSettings(); renderBuckets(); });
    }
  };
  bindResist('#s-phys', 'physResist');
  bindResist('#s-sin', 'sinResist');
}

// ── Bucket Event Delegation ──

function setupBucketEvents() {
  const el = document.getElementById('buckets');

  // Skill card input changes — targeted update
  el.addEventListener('input', e => {
    const f = e.target.dataset.f;
    const card = e.target.closest('[data-entry-id]');
    if (f && card) {
      handleSkillInput(card.dataset.entryId, f, e.target.value);
      return;
    }
    const bucketEl = e.target.closest('[data-bucket-id]');
    if (e.target.classList.contains('bucket-name-input') && bucketEl) {
      const bucket = state.buckets.find(b => b.id === bucketEl.dataset.bucketId);
      if (bucket) bucket.name = e.target.value;
      updateComparison();
    }
  });

  // Select changes
  el.addEventListener('change', e => {
    const f = e.target.dataset.f;
    const card = e.target.closest('[data-entry-id]');
    if (f && card) handleSkillInput(card.dataset.entryId, f, e.target.value);
  });

  // Clicks
  el.addEventListener('click', e => {
    const removeBtn = e.target.closest('[data-skill-remove]');
    if (removeBtn) {
      const entryId = removeBtn.dataset.skillRemove;
      for (const bucket of state.buckets) bucket.skills = bucket.skills.filter(s => s.id !== entryId);
      renderBuckets();
      return;
    }
    // Save skill to custom sinner
    const saveBtn = e.target.closest('[data-skill-save]');
    if (saveBtn) {
      const entryId = saveBtn.dataset.skillSave;
      const entry = findEntry(entryId);
      if (entry) {
        const customs = loadCustomSkills();
        const copy = JSON.parse(JSON.stringify(entry.skill));
        copy.label = 'Custom';
        customs.push(copy);
        saveCustomSkills(customs);
      }
      return;
    }
    if (e.target.closest('.bucket-remove')) {
      const bucketEl = e.target.closest('[data-bucket-id]');
      if (bucketEl && state.buckets.length > 1) {
        state.buckets = state.buckets.filter(b => b.id !== bucketEl.dataset.bucketId);
        renderBuckets();
      }
      return;
    }
    if (e.target.id === 'add-bucket-btn') {
      state.buckets.push({ id: uid(), name: `Group ${state.buckets.length + 1}`, skills: [] });
      renderBuckets();
      return;
    }
    if (e.target.id === 'add-custom-btn') {
      addBlankSkill();
      return;
    }
  });

  // Drag and drop
  el.addEventListener('dragstart', e => {
    const card = e.target.closest('[data-drag-skill]');
    if (!card) return;
    const bucketEl = card.closest('[data-bucket-id]');
    e.dataTransfer.setData('text/plain', JSON.stringify({
      skillId: card.dataset.dragSkill,
      sourceBucketId: bucketEl?.dataset.bucketId,
    }));
    card.classList.add('dragging');
  });
  el.addEventListener('dragend', e => {
    const card = e.target.closest('[data-drag-skill]');
    if (card) card.classList.remove('dragging');
  });
  el.addEventListener('dragover', e => {
    const zone = e.target.closest('[data-drop-bucket]');
    if (zone) { e.preventDefault(); zone.classList.add('drop-target'); }
  });
  el.addEventListener('dragleave', e => {
    const zone = e.target.closest('[data-drop-bucket]');
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove('drop-target');
  });
  el.addEventListener('drop', e => {
    const zone = e.target.closest('[data-drop-bucket]');
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove('drop-target');
    try {
      const { skillId, sourceBucketId } = JSON.parse(e.dataTransfer.getData('text/plain'));
      moveSkill(skillId, sourceBucketId, zone.dataset.dropBucket, e.target);
    } catch {}
  });
}

// ── Skill Input Handler ──

function handleSkillInput(entryId, field, value) {
  const entry = findEntry(entryId);
  if (!entry) return;
  const skill = entry.skill;

  // Skill name
  if (field === 'skill_name') {
    skill.name = value;
    return; // cosmetic only, no recalc needed
  }

  // Per-coin fields: coin_X_field
  const coinMatch = field.match(/^coin_(\d+)_(\w+)$/);
  if (coinMatch) {
    const ci = parseInt(coinMatch[1]);
    const cf = coinMatch[2];
    while (skill.coins.length <= ci) skill.coins.push({ coinValueOverride: '', dmgBonusAdd: '', extraHitPct: '' });
    skill.coins[ci][cf] = value;
    updateCardDmg(entryId);
    updateComparison();
    return;
  }

  switch (field) {
    case 'base_power': skill.base_power = parseInt(value) || 0; break;
    case 'coin_value':
      skill.coin_value = parseInt(value) || 0;
      break;
    case 'num_coins':
      skill.num_coins = Math.max(1, Math.min(9, parseInt(value) || 1));
      while (skill.coins.length < skill.num_coins) skill.coins.push({ coinValueOverride: '', dmgBonusAdd: '', extraHitPct: '' });
      renderBuckets(); // structural change — need full re-render for coin rows
      return;
    case 'skill_dmg_bonus': skill.skill_dmg_bonus = parseInt(value) || 0; break;
    case 'offense_level_offset': skill.offense_level_offset = parseInt(value) || 0; break;
    case 'atk_weight': skill.atk_weight = Math.max(1, parseInt(value) || 1); break;
  }

  updateCardDmg(entryId);
  updateComparison();
}

// ── Targeted DOM Updates ──

function updateCardDmg(entryId) {
  const entry = findEntry(entryId);
  if (!entry) return;
  const skill = entry.skill;
  const combined = getCombined();
  const hasBuff = hasBuffValues(combined);
  const data = calcDamage(skill, null, state.settings);
  const buffData = hasBuff ? calcDamage(skill, combined, state.settings) : null;
  const wt = skill.atk_weight || 1;

  const dmgEl = document.getElementById(`dmg-${entryId}`);
  if (dmgEl) {
    const baseCells = dmgEl.querySelectorAll('.dtbl-edit .fg-base');
    const rawCells = dmgEl.querySelectorAll('.dtbl-edit .fg-raw');
    const poolCells = dmgEl.querySelectorAll('.dtbl-edit .fg-pool');
    const dmgCells = dmgEl.querySelectorAll('.dtbl-edit .fg-dmg');
    const totCell = dmgEl.querySelector('.fg-tot-val');

    if (dmgCells.length === data.rows.length) {
      for (let i = 0; i < data.rows.length; i++) {
        const r = data.rows[i];
        if (baseCells[i]) baseCells[i].textContent = fmt(r.prevRaw);
        if (rawCells[i]) rawCells[i].textContent = fmt(r.raw);
        if (poolCells[i]) poolCells[i].innerHTML = poolDisplay(r.pool, wt);
        dmgCells[i].textContent = fmt(r.coinTotal);
      }
      if (totCell) totCell.textContent = fmt(data.total);

      // Update buff table cells in-place (never touch the edit table)
      const buffTable = dmgEl.querySelector('.dtbl-buff');
      if (buffData && buffTable) {
        const fbBase = buffTable.querySelectorAll('.fb-base');
        const fbCoin = buffTable.querySelectorAll('.fb-coin');
        const fbRaw = buffTable.querySelectorAll('.fb-raw');
        const fbDmgPct = buffTable.querySelectorAll('.fb-dmgpct');
        const fbPool = buffTable.querySelectorAll('.fb-pool');
        const fbDmg = buffTable.querySelectorAll('.fb-dmg');
        const fbTot = buffTable.querySelector('.fb-tot-val');

        if (fbDmg.length === buffData.rows.length) {
          for (let i = 0; i < buffData.rows.length; i++) {
            const r = buffData.rows[i];
            const c = skill.coins?.[i] || {};
            const coinDmg = parseFloat(c.dmgBonusAdd) || 0;
            if (fbBase[i]) fbBase[i].textContent = fmt(r.prevRaw);
            if (fbCoin[i]) fbCoin[i].textContent = fmt(r.coinVal);
            if (fbRaw[i]) fbRaw[i].textContent = fmt(r.raw);
            if (fbDmgPct[i]) fbDmgPct[i].textContent = coinDmg ? fmt(coinDmg) : '0';
            if (fbPool[i]) fbPool[i].innerHTML = poolDisplay(r.pool, wt);
            if (fbDmg[i]) fbDmg[i].textContent = fmt(r.coinTotal);
          }
          if (fbTot) {
            const diff = buffData.total - data.total;
            fbTot.innerHTML = `${fmt(buffData.total)}${diff ? ` <span class="accent">+${fmt(diff)}</span>` : ''}`;
          }
        } else {
          // Row count changed — structural re-render of buff table only
          const temp = document.createElement('div');
          temp.innerHTML = renderDmgTables(skill, data, buffData, combined);
          const newBuff = temp.querySelector('.dtbl-buff');
          if (newBuff) buffTable.replaceWith(newBuff);
        }
      } else if (buffData && !buffTable) {
        // Buff just appeared — append it
        const temp = document.createElement('div');
        temp.innerHTML = renderDmgTables(skill, data, buffData, combined);
        const newBuff = temp.querySelector('.dtbl-buff');
        if (newBuff) dmgEl.querySelector('.tables-wrap')?.appendChild(newBuff);
      } else if (!buffData && buffTable) {
        buffTable.remove();
      }
    } else {
      // Save focus state before innerHTML replacement
      const focused = document.activeElement;
      const focusField = focused?.dataset?.f;
      const focusEntry = focused?.closest('[data-entry-id]')?.dataset?.entryId;
      const focusPos = focused?.selectionStart;

      dmgEl.innerHTML = renderDmgTables(skill, data, buffData, combined);

      // Restore focus if it was inside this card
      if (focusField && focusEntry === entryId) {
        const restored = dmgEl.querySelector(`[data-f="${focusField}"]`);
        if (restored) { restored.focus(); if (focusPos != null) try { restored.setSelectionRange(focusPos, focusPos); } catch {} }
      }
    }
  }

  const formulaEl = document.getElementById(`formula-${entryId}`);
  if (formulaEl) {
    const coinSign = skill.coin_value >= 0 ? '+' : '';
    formulaEl.textContent = `${skill.base_power}${coinSign}${skill.coin_value}\u00d7${skill.num_coins}${skill.coin_value < 0 ? ' (minus)' : ''} \u00b7 Weight:${skill.atk_weight}`;
  }
}

function updateComparison() {
  const el = document.getElementById('comparison');
  if (!el) return;
  const combined = getCombined();
  const hasBuff = hasBuffValues(combined);

  const summaries = state.buckets.map(bucket => {
    let total = 0, buffTotal = 0;
    for (const entry of bucket.skills) {
      const data = calcDamage(entry.skill, null, state.settings);
      total += data.total;
      if (hasBuff) {
        const bd = calcDamage(entry.skill, combined, state.settings);
        buffTotal += bd.total;
      }
    }
    return { id: bucket.id, name: bucket.name, count: bucket.skills.length, total, buffTotal: hasBuff ? buffTotal : null, diff: hasBuff ? buffTotal - total : 0 };
  });

  const active = summaries.filter(b => b.count > 0);
  if (active.length === 0) { el.style.display = 'none'; return; }
  el.style.display = '';

  el.innerHTML = `
    <div class="section-header"><span class="section-title">Comparison</span></div>
    <div class="table-grid ${hasBuff ? 'cols-4' : 'cols-2'}">
      <span class="th">Bucket</span><span class="th right">Total</span>
      ${hasBuff ? '<span class="th right">Buffed</span><span class="th right accent">Diff</span>' : ''}
      ${active.map(b => `
        <span>${esc(b.name)} (${b.count})</span>
        <span class="right mono bold">${fmt(b.total)}</span>
        ${hasBuff ? `<span class="right mono">${fmt(b.buffTotal)}</span><span class="right mono accent">+${fmt(b.diff)}</span>` : ''}
      `).join('')}
    </div>`;
}

// ── Buckets ──

function renderBuckets() {
  const el = document.getElementById('buckets');
  const combined = getCombined();
  const hasBuff = hasBuffValues(combined);

  let html = `<div id="comparison" class="comparison-table card"></div>`;
  for (const bucket of state.buckets) html += renderBucket(bucket, combined, hasBuff);
  html += `<div class="bottom-actions">
    <button id="add-bucket-btn" class="btn btn-secondary">+ Add Bucket</button>
    <button id="add-custom-btn" class="btn btn-secondary">+ Custom Skill</button>
  </div>
  <div class="footer-note">Total% includes offense level modifier and physical &amp; sin resistances. Base crit (+20%) is assumed and not shown.</div>`;
  el.innerHTML = html;
  updateComparison();
}

function renderBucket(bucket, combined, hasBuff) {
  const canRemove = state.buckets.length > 1;
  let html = `<div class="bucket card" data-bucket-id="${bucket.id}">
    <div class="bucket-header">
      <input class="bucket-name-input" value="${esc(bucket.name)}">
      ${canRemove ? '<button class="btn-icon bucket-remove" title="Remove bucket">&times;</button>' : ''}
    </div>
    <div class="skill-list" data-drop-bucket="${bucket.id}">`;
  if (bucket.skills.length === 0) {
    html += `<div class="empty-state">Add skills using the picker above — or drag skills here</div>`;
  }
  for (const entry of bucket.skills) html += renderSkillCard(entry, combined, hasBuff);
  html += `</div></div>`;
  return html;
}

// ── Skill Card ──

function renderSkillCard(entry, combined, hasBuff) {
  const { skill, identityName } = entry;
  const data = calcDamage(skill, null, state.settings);
  const buffData = hasBuff ? calcDamage(skill, combined, state.settings) : null;
  const coinSign = skill.coin_value >= 0 ? '+' : '';

  return `<div class="skill-card" draggable="true" data-drag-skill="${entry.id}" data-entry-id="${entry.id}">
    <div class="skill-header">
      <div class="skill-identity">${esc(identityName)}</div>
      <div class="skill-header-actions">
        <button class="btn-save" data-skill-save="${entry.id}" title="Save to Custom sinner">Save</button>
        <button class="btn-icon" data-skill-remove="${entry.id}" title="Remove">&times;</button>
      </div>
    </div>
    <div class="skill-title">
      <span class="skill-label-lg">${esc(skill.label)}</span>
      <input class="skill-name-lg" data-f="skill_name" value="${esc(skill.name)}" placeholder="Skill name">
    </div>
    <div class="skill-inputs">
      <div class="stat-row-lg">
        <label class="stat-field-lg">Base Pw <input class="stat-input" data-f="base_power" type="number" value="${skill.base_power}"></label>
        <label class="stat-field-lg">Coin Pw <input class="stat-input" data-f="coin_value" type="number" value="${skill.coin_value}"></label>
        <label class="stat-field-lg">Coins <input class="stat-input" data-f="num_coins" type="number" value="${skill.num_coins}" min="1" max="9"></label>
        <label class="stat-field-lg">+Damage% <input class="stat-input" data-f="skill_dmg_bonus" type="number" value="${skill.skill_dmg_bonus || 0}"></label>
        <label class="stat-field-lg">Atk Wt <input class="stat-input" data-f="atk_weight" type="number" value="${skill.atk_weight}" min="1"></label>
        <label class="stat-field-lg">+Offense <input class="stat-input" data-f="offense_level_offset" type="number" value="${skill.offense_level_offset}"></label>
      </div>
    </div>
    <div class="skill-formula" id="formula-${entry.id}">${skill.base_power}${coinSign}${skill.coin_value}&times;${skill.num_coins}${skill.coin_value < 0 ? ' (minus)' : ''} &middot; Weight:${skill.atk_weight}</div>
    <div id="dmg-${entry.id}">${renderDmgTables(skill, data, buffData, combined)}</div>
  </div>`;
}

// ── Damage Tables ──

function poolDisplay(pool, weight) {
  const p = `+${Math.round(pool)}%`;
  return weight > 1 ? `${p} &times;${weight}` : p;
}

function renderDmgTables(skill, data, buffData, combined) {
  const wt = skill.atk_weight || 1;
  const cls = wt > 1 ? 'dtbl-cols-8' : 'dtbl-cols-7';
  const wtHdr = wt > 1 ? '<span class="dtbl-hdr">&times;Wt</span>' : '';
  let html = '<div class="tables-wrap">';

  // Editable table
  html += `<div class="dtbl dtbl-edit">
    <div class="dtbl-title">Damage</div>
    <div class="dtbl-grid ${cls}">
      <span class="dtbl-hdr">#</span><span class="dtbl-hdr">Base</span><span class="dtbl-hdr">Coin</span><span class="dtbl-hdr">Raw</span><span class="dtbl-hdr">+Dmg%</span><span class="dtbl-hdr">Total%</span>${wtHdr}<span class="dtbl-hdr right">Dmg</span>
      <div class="dtbl-sep"></div>`;

  for (let i = 0; i < data.rows.length; i++) {
    const r = data.rows[i];
    const c = skill.coins?.[i] || {};
    html += `
      <span class="coin-num">${r.n}</span>
      <span class="fg-base muted">${fmt(r.prevRaw)}</span>
      <input class="stat-input fg-input" data-f="coin_${i}_coinValueOverride" type="number" value="${c.coinValueOverride || ''}" placeholder="${skill.coin_value}">
      <span class="fg-raw muted">${fmt(r.raw)}</span>
      <input class="stat-input fg-input" data-f="coin_${i}_dmgBonusAdd" type="number" value="${c.dmgBonusAdd || ''}" placeholder="0">
      <span class="fg-pool muted">${poolDisplay(r.pool, wt)}</span>
      ${wt > 1 ? `<span class="fg-wt muted">&times;${wt}</span>` : ''}
      <span class="fg-dmg right bold">${fmt(r.coinTotal)}</span>`;
  }

  html += `<div class="dtbl-sep-tot"></div>
    <span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span>${wt > 1 ? '<span class="dtbl-tot"></span>' : ''}
    <span class="dtbl-tot right fg-tot-val">${fmt(data.total)}</span>`;
  html += `</div></div>`;

  // Buff table (always shown — mirrors data when no buffs)
  const bRows = buffData ? buffData.rows : data.rows;
  const bTotal = buffData ? buffData.total : data.total;
  const bLabel = buffData ? `w/ ${esc(combined.label || 'Buffs')}` : 'w/ Nothing';
  const diff = bTotal - data.total;

  html += `<div class="dtbl dtbl-buff">
    <div class="dtbl-title purple">${bLabel}</div>
    <div class="dtbl-grid ${cls}">
      <span class="dtbl-hdr">#</span><span class="dtbl-hdr">Base</span><span class="dtbl-hdr">Coin</span><span class="dtbl-hdr">Raw</span><span class="dtbl-hdr">+Dmg%</span><span class="dtbl-hdr">Total%</span>${wtHdr}<span class="dtbl-hdr right">Dmg</span>
      <div class="dtbl-sep"></div>`;

  for (let i = 0; i < bRows.length; i++) {
    const r = bRows[i];
    const c = skill.coins?.[i] || {};
    const coinDmg = parseFloat(c.dmgBonusAdd) || 0;
    html += `
      <span class="coin-num">${r.n}</span>
      <span class="fb-base muted">${fmt(r.prevRaw)}</span>
      <span class="fb-coin">${fmt(r.coinVal)}</span>
      <span class="fb-raw muted">${fmt(r.raw)}</span>
      <span class="fb-dmgpct">${coinDmg ? fmt(coinDmg) : '0'}</span>
      <span class="fb-pool muted">${poolDisplay(r.pool, wt)}</span>
      ${wt > 1 ? `<span class="fb-wt muted">&times;${wt}</span>` : ''}
      <span class="fb-dmg right bold">${fmt(r.coinTotal)}</span>`;
  }

  html += `<div class="dtbl-sep-tot"></div>
    <span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span><span class="dtbl-tot"></span>${wt > 1 ? '<span class="dtbl-tot"></span>' : ''}
    <span class="dtbl-tot right fb-tot-val">${fmt(bTotal)}${diff ? ` <span class="accent">+${fmt(diff)}</span>` : ''}</span>`;
  html += `</div></div>`;

  html += '</div>';
  return html;
}

// ── Add blank custom skill ──

function addBlankSkill() {
  const bucket = state.buckets[state.buckets.length - 1];
  bucket.skills.push({
    id: uid(),
    identityName: 'Custom',
    sinner: '',
    tier: null,
    skill: {
      label: 'Custom',
      name: '',
      base_power: 0,
      coin_value: 0,
      num_coins: 1,
      damage_type: 'Slash',
      sin_affinity: '',
      offense_level_offset: 0,
      atk_weight: 1,
      skill_dmg_bonus: 0,
      coins: [{ coinValueOverride: '', dmgBonusAdd: '', extraHitPct: '' }],
    },
  });
  renderBuckets();
}
