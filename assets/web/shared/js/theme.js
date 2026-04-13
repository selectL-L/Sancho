/**
 * @fileoverview Shared theme utilities for all web pages.
 * 
 * Handles system theme detection, theme application, and CSS variable injection
 * for the theme color from the API.
 * 
 * @module web-core/theme
 */

// ============================================================================
// THEME DETECTION
// ============================================================================

/**
 * Get the user's preferred color scheme.
 * 
 * Checks localStorage first, then falls back to system preference.
 * 
 * @returns {'light' | 'dark'} Preferred theme
 */
export function getPreferredTheme() {
  // Check localStorage for explicit preference
  const stored = localStorage.getItem('theme');
  if (stored === 'light' || stored === 'dark') {
    return stored;
  }
  
  // Fall back to system preference
  if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    return 'dark';
  }
  
  return 'light';
}

/**
 * Save theme preference to localStorage.
 * 
 * @param {'light' | 'dark'} theme - Theme to save
 */
export function saveThemePreference(theme) {
  localStorage.setItem('theme', theme);
}

// ============================================================================
// THEME APPLICATION
// ============================================================================

/**
 * Apply a theme to the document.
 * 
 * Sets the appropriate class on the <body> element.
 * This matches the original monolithic implementation which uses
 * document.body.classList.toggle() for theme switching.
 * 
 * The CSS uses bare .dark/.light selectors which work on any element,
 * but the HTML files start with <body class="dark">, so we must
 * manage the class on body to maintain consistency.
 * 
 * @param {'light' | 'dark'} theme - Theme to apply
 */
export function applyTheme(theme) {
  const body = document.body;
  if (!body) return;
  
  // Apply to body only (matching original monolithic behavior)
  body.classList.remove('light', 'dark');
  body.classList.add(theme);
  
}

/**
 * Toggle between light and dark themes.
 * 
 * @returns {'light' | 'dark'} The new theme
 */
export function toggleTheme() {
  const body = document.body;
  const current = body.classList.contains('dark') ? 'dark' : 'light';
  const newTheme = current === 'dark' ? 'light' : 'dark';
  
  applyTheme(newTheme);
  saveThemePreference(newTheme);
  
  return newTheme;
}

// ============================================================================
// THEME COLOR
// ============================================================================

/**
 * Parse a hex color to RGB components.
 * 
 * @param {string} hex - Hex color like "#9333ea", "9333ea", "#fff", or "9e3"
 * @returns {{ r: number, g: number, b: number } | null} RGB components or null if invalid
 */
export function parseHexColor(hex) {
  // Remove # if present
  let clean = hex.replace(/^#/, '');

  // Expand 3-char shorthand to 6-char form (e.g. "fff" -> "ffffff")
  if (clean.length === 3) {
    clean = clean[0] + clean[0] + clean[1] + clean[1] + clean[2] + clean[2];
  }

  if (clean.length !== 6) {
    return null;
  }
  
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  
  if (isNaN(r) || isNaN(g) || isNaN(b)) {
    return null;
  }
  
  return { r, g, b };
}

/**
 * Set the theme color CSS variables.
 * 
 * This sets --theme-r, --theme-g, --theme-b which are used for
 * heatmap colors and other theme-colored elements.
 * 
 * @param {string} hex - Hex color like "#9333ea"
 */
export function setThemeColor(hex) {
  const rgb = parseHexColor(hex);
  
  if (!rgb) {
    console.warn('Invalid theme color:', hex);
    return;
  }
  
  const root = document.documentElement;
  root.style.setProperty('--theme-r', String(rgb.r));
  root.style.setProperty('--theme-g', String(rgb.g));
  root.style.setProperty('--theme-b', String(rgb.b));
}

/**
 * Get the current theme color RGB values from CSS variables.
 * 
 * @returns {{ r: number, g: number, b: number }} RGB components
 */
export function getThemeColorRGB() {
  const styles = getComputedStyle(document.documentElement);
  return {
    r: parseInt(styles.getPropertyValue('--theme-r').trim(), 10) || 147,
    g: parseInt(styles.getPropertyValue('--theme-g').trim(), 10) || 51,
    b: parseInt(styles.getPropertyValue('--theme-b').trim(), 10) || 234,
  };
}

// ============================================================================
// INITIALIZATION
// ============================================================================

/**
 * Initialize theme on page load.
 * 
 * Call this early in page initialization to prevent flash of wrong theme.
 * Also sets up system theme change listener.
 * 
 * @param {string} [themeColor] - Optional theme color from API config
 */
export function initTheme(themeColor) {
  // Apply preferred theme immediately
  const theme = getPreferredTheme();
  applyTheme(theme);
  
  // Set theme color if provided
  if (themeColor) {
    setThemeColor(themeColor);
  }
  
  // Listen for system theme changes
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
      // Only auto-switch if user hasn't set an explicit preference
      if (!localStorage.getItem('theme')) {
        applyTheme(e.matches ? 'dark' : 'light');
      }
    });
  }
}
