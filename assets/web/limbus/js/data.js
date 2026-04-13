/**
 * data.js — Identity data loading and filtering.
 * Loads identities.json and provides lookup/filter functions.
 */

let _data = null;
let _byId = new Map();
let _bySinner = new Map();

/**
 * Load identities.json from the API.
 * @param {string} [url='/api/identities']
 * @returns {Promise<object>} The full dataset
 */
export async function loadData(url = '/api/identities') {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`Failed to load ${url}: ${resp.status}`);
  _data = await resp.json();

  // Build indexes
  _byId.clear();
  _bySinner.clear();

  for (const identity of _data.identities) {
    _byId.set(identity.id, identity);

    const sinner = identity.sinner || 'Unknown';
    if (!_bySinner.has(sinner)) _bySinner.set(sinner, []);
    _bySinner.get(sinner).push(identity);
  }

  return _data;
}

/** @returns {Array} All identities */
export function getAllIdentities() {
  return _data?.identities || [];
}

/** @returns {Array<string>} All sinner names, sorted */
export function getSinners() {
  return [..._bySinner.keys()].sort();
}

/**
 * Get identities for a sinner.
 * @param {string} sinner
 * @returns {Array}
 */
export function getIdentitiesForSinner(sinner) {
  return _bySinner.get(sinner) || [];
}

/**
 * Get a single identity by ID.
 * @param {string} id
 * @returns {object|undefined}
 */
export function getIdentityById(id) {
  return _byId.get(id);
}

/**
 * Search identities by name (case-insensitive substring match).
 * @param {string} query
 * @returns {Array}
 */
function searchIdentities(query) {
  if (!query) return getAllIdentities();
  const q = query.toLowerCase();
  return getAllIdentities().filter(i =>
    i.name.toLowerCase().includes(q) || i.sinner.toLowerCase().includes(q)
  );
}

/** @returns {string} Scrape timestamp */
function getScrapedAt() {
  return _data?.scraped_at || '';
}

/** @returns {number} Total identity count */
function getIdentityCount() {
  return _data?.identity_count || 0;
}

/**
 * Built-in buff presets (EGO Gifts, common buffs).
 */
const BUFF_PRESETS = [
  { name: '2 Damage Up', basePowerAdd: 0, coinPowerAdd: 0, dmgAdd: 20 },
  { name: '4 Damage Up', basePowerAdd: 0, coinPowerAdd: 0, dmgAdd: 40 },
  { name: 'Puncture (+2 Base, +1 Coin, +50%)', basePowerAdd: 2, coinPowerAdd: 1, dmgAdd: 50 },
  { name: 'Legerdemain (+3 Coin)', basePowerAdd: 0, coinPowerAdd: 3, dmgAdd: 0 },
  { name: 'Custom', basePowerAdd: 0, coinPowerAdd: 0, dmgAdd: 0 },
];
