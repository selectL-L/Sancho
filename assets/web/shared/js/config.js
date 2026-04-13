/**
 * config.js — Shared bot configuration and branding.
 * Fetches bot name and theme color from /api/config,
 * applies branding footer and theme color to the page.
 *
 * @module shared/config
 */

/**
 * Fetch bot configuration from the API.
 * @returns {Promise<{botName: string, themeColor: string|null}>}
 */
export async function fetchConfig() {
  try {
    const resp = await fetch('/api/config');
    if (!resp.ok) return { botName: 'Bot', themeColor: null };
    return await resp.json();
  } catch {
    return { botName: 'Bot', themeColor: null };
  }
}

/**
 * Apply "served to you by {botName}" branding to an element.
 * @param {string} elementId - The DOM element ID to populate.
 * @param {string} botName - The bot's display name.
 */
export function applyBranding(elementId, botName) {
  const el = document.getElementById(elementId);
  if (el) el.textContent = `served to you by ${botName}`;
}

/**
 * Initialize config: fetch, apply branding, and optionally set theme color.
 * @param {string} brandingElementId - Element ID for the branding text.
 * @param {function} [setThemeColor] - Optional callback to set theme color (from theme.js).
 * @returns {Promise<{botName: string, themeColor: string|null}>}
 */
export async function initConfig(brandingElementId, setThemeColor) {
  const config = await fetchConfig();
  applyBranding(brandingElementId, config.botName || 'Bot');
  if (config.themeColor && setThemeColor) {
    setThemeColor(config.themeColor);
  }
  return config;
}
