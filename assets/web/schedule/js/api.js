/**
 * @fileoverview API wrapper for the availability web UI.
 * 
 * Single source of truth for all API calls. Handles:
 * - Fetch with credentials
 * - Auth error detection (401 → redirect to login)
 * - Error normalization
 * - Response parsing
 * 
 * All functions use canonical names defined in WEB_REFACTOR_4B_PLAN.md.
 * 
 * @module web-core/api
 */

// ============================================================================
// CONFIGURATION
// ============================================================================

/** Base URL for API endpoints (empty = same origin) */
const API_BASE = '';

/** Whether to log API calls to console (dev mode) */
const DEBUG = false;

// ============================================================================
// CORE FETCH WRAPPER
// ============================================================================

/**
 * Make an authenticated API request.
 * 
 * Handles common error cases:
 * - 401: Redirects to login
 * - Network errors: Throws with message
 * - Non-2xx: Throws with error from response body
 * 
 * @param {string} endpoint - API endpoint (e.g., '/api/guilds')
 * @param {RequestInit} [options={}] - Fetch options
 * @returns {Promise<any>} Parsed JSON response
 * @throws {Error} On network or API error
 */
async function apiFetch(endpoint, options = {}) {
  const url = `${API_BASE}${endpoint}`;
  
  const config = {
    credentials: 'include',
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  };
  
  if (DEBUG) {
    console.log(`[API] ${config.method || 'GET'} ${endpoint}`, options.body || '');
  }
  
  let response;
  try {
    response = await fetch(url, config);
  } catch (e) {
    throw new Error('Network error - please check your connection');
  }
  
  // Handle 401 - redirect to login
  if (response.status === 401) {
    // Don't redirect if we're already on the index (login) page
    if (!window.location.pathname.includes('index')) {
      window.location.href = '/index.html?error=session_expired';
    }
    throw new Error('Session expired');
  }
  
  // Parse response body
  let data;
  const contentType = response.headers.get('content-type');
  if (contentType && contentType.includes('application/json')) {
    data = await response.json();
  } else {
    data = await response.text();
  }
  
  if (DEBUG) {
    console.log(`[API] Response ${response.status}:`, data);
  }
  
  // Handle non-2xx responses
  if (!response.ok) {
    const errorMessage = (typeof data === 'object' && data.error) 
      ? data.error 
      : `Request failed (${response.status})`;
    throw new Error(errorMessage);
  }
  
  return data;
}

// ============================================================================
// AUTH ENDPOINTS
// ============================================================================

/**
 * Get the current logged-in user.
 * 
 * @returns {Promise<User | null>} User object or null if not logged in
 * 
 * @typedef {Object} User
 * @property {string} id - Discord user ID
 * @property {string} username - Display name
 * @property {string | null} avatar - Avatar URL
 * @property {string | null} timezone - User's timezone (display format)
 * @property {boolean} hasTimezone - Whether timezone is set
 */
export async function getMe() {
  try {
    return await apiFetch('/auth/me');
  } catch (e) {
    // 401 is expected when not logged in
    if (e.message === 'Session expired') {
      return null;
    }
    throw e;
  }
}

/**
 * Redirect to logout endpoint.
 * 
 * @param {boolean} [clearAll=false] - If true, clears all sessions for user
 */
export function logout(clearAll = false) {
  const url = clearAll ? '/auth/logout?clear=true' : '/auth/logout';
  window.location.href = url;
}

/**
 * Redirect to login endpoint.
 */
export function login() {
  window.location.href = '/auth/login';
}

// ============================================================================
// CONFIG ENDPOINT
// ============================================================================

/**
 * Get public configuration.
 * 
 * @returns {Promise<Config>} Config object
 * 
 * @typedef {Object} Config
 * @property {string} botName - Bot display name
 * @property {string} [themeColor] - Hex color for theming
 */
export async function getConfig() {
  return await apiFetch('/api/config');
}

// ============================================================================
// AVAILABILITY ENDPOINTS
// ============================================================================

/**
 * Get a user's availability slots.
 * 
 * @param {string} userId - Target user ID (use 'me' or actual ID)
 * @returns {Promise<AvailabilityResponse>} Availability data
 * 
 * @typedef {Object} AvailabilityResponse
 * @property {string[]} slots - Array of slot keys like "mon-0900"
 * @property {string} [username] - Target user's display name
 * @property {string | null} [avatar] - Target user's avatar URL
 * @property {string | null} [updatedAt] - ISO timestamp of last update
 * @property {boolean} [timezoneConverted] - Whether slots were converted
 * @property {string | null} [sourceTimezone] - Original timezone if not converted
 */
export async function getUserAvailability(userId) {
  return await apiFetch(`/api/users/${userId}/availability`);
}

/**
 * Get the current user's own availability.
 * 
 * Convenience wrapper that uses 'me' as the user ID.
 * 
 * @returns {Promise<AvailabilityResponse>} Availability data
 */
export async function getMyAvailability() {
  // The API uses the session user when fetching own ID
  const me = await getMe();
  if (!me) throw new Error('Not logged in');
  return await getUserAvailability(me.id);
}

/**
 * Save the current user's availability.
 * 
 * This replaces all existing availability with the provided slots.
 * 
 * @param {string[]} slots - Array of slot keys to save
 * @returns {Promise<{ success: boolean, count: number }>} Result
 */
export async function saveAvailability(slots) {
  return await apiFetch('/api/availability', {
    method: 'POST',
    body: JSON.stringify({ slots }),
  });
}

/**
 * Clear all availability for the current user.
 * 
 * @returns {Promise<{ success: boolean }>} Result
 */
export async function clearAvailability() {
  return await apiFetch('/api/availability', {
    method: 'DELETE',
  });
}

// ============================================================================
// GUILD ENDPOINTS
// ============================================================================

/**
 * Get the user's guilds with visibility settings.
 * 
 * @returns {Promise<{ guilds: Guild[] }>} Guilds data
 * 
 * @typedef {Object} Guild
 * @property {string} id - Guild ID
 * @property {string} name - Guild name
 * @property {string | null} icon - Guild icon URL
 * @property {boolean} enabled - Visibility enabled
 * @property {number} [memberCount] - Total member count
 */
export async function getGuilds() {
  return await apiFetch('/api/guilds');
}

/**
 * Set visibility for a specific guild.
 * 
 * @param {string} guildId - Guild ID
 * @param {boolean} visible - Whether to be visible in this guild
 * @returns {Promise<{ success: boolean }>} Result
 */
export async function setGuildVisibility(guildId, visible) {
  return await apiFetch(`/api/guilds/${guildId}/visibility`, {
    method: 'POST',
    body: JSON.stringify({ visible }),
  });
}

/**
 * Get users viewable in a specific guild.
 * 
 * @param {string} guildId - Guild ID
 * @returns {Promise<{ users: User[], guildName: string }>} Viewable users
 */
export async function getViewableUsers(guildId) {
  return await apiFetch(`/api/guilds/${guildId}/viewable-users`);
}

/**
 * Get all viewable users across all guilds.
 * 
 * @param {string} [focusGuild='0'] - Guild ID to prioritize in sorting
 * @returns {Promise<{ users: User[] }>} All viewable users with shared_guilds
 */
export async function getAllViewableUsers(focusGuild = '0') {
  const params = focusGuild !== '0' ? `?focus_guild=${focusGuild}` : '';
  return await apiFetch(`/api/all-viewable-users${params}`);
}

// ============================================================================
// BLACKLIST ENDPOINTS
// ============================================================================

/**
 * Get the current user's blacklist.
 * 
 * @returns {Promise<{ users: User[] }>} Blocked users
 */
export async function getBlacklist() {
  return await apiFetch('/api/blacklist');
}

/**
 * Add a user to the blacklist.
 * 
 * @param {string} userId - User ID to block
 * @returns {Promise<{ success: boolean }>} Result
 */
export async function blockUser(userId) {
  return await apiFetch(`/api/blacklist/${userId}`, {
    method: 'POST',
  });
}

/**
 * Remove a user from the blacklist.
 * 
 * @param {string} userId - User ID to unblock
 * @returns {Promise<{ success: boolean }>} Result
 */
export async function unblockUser(userId) {
  return await apiFetch(`/api/blacklist/${userId}`, {
    method: 'DELETE',
  });
}

/**
 * Search for a user by name or ID.
 * 
 * @param {string} query - Username or user ID to search for
 * @returns {Promise<ResolveResult>} Search result
 * 
 * @typedef {Object} ResolveResult
 * @property {boolean} found - Whether a user was found
 * @property {string} [id] - User ID if found
 * @property {string} [username] - Username if found
 * @property {string | null} [avatar] - Avatar URL if found
 * @property {boolean} [multiple] - If true, matches array has multiple results
 * @property {User[]} [matches] - Array of matching users if multiple
 * @property {string} [message] - Status message
 * @property {string} [note] - Additional note (e.g., user not in shared servers)
 */
export async function resolveUser(query) {
  const encoded = encodeURIComponent(query);
  return await apiFetch(`/api/users/resolve?query=${encoded}`);
}

// ============================================================================
// ACCOUNT ENDPOINTS
// ============================================================================

/**
 * Delete all user data.
 * 
 * This is destructive and cannot be undone!
 * 
 * @returns {Promise<{ success: boolean }>} Result
 */
export async function deleteAccount() {
  return await apiFetch('/api/account', {
    method: 'DELETE',
  });
}
