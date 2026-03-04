/**
 * @fileoverview Reactive state management for the availability web UI.
 * 
 * Provides a simple reactive state system with subscriptions.
 * Shells subscribe to state changes and update their UI accordingly.
 * 
 * All state keys use canonical names defined in WEB_REFACTOR_4B_PLAN.md.
 * 
 * @module web-core/state
 */

// ============================================================================
// STATE STORE
// ============================================================================

/**
 * The global state object.
 * 
 * DO NOT modify directly - use setState() to trigger subscriptions.
 * 
 * @type {State}
 */
const state = {
  // ---- Auth ----
  /** @type {User | null} Current logged-in user */
  currentUser: null,
  
  /** @type {boolean} Whether user is logged in */
  isLoggedIn: false,
  
  // ---- Config ----
  /** @type {Config} Public config from API */
  config: {
    botName: 'Shiori',
    themeColor: null,
  },
  
  /** @type {'light' | 'dark'} Current theme */
  theme: 'dark',
  
  // ---- Index Page State ----
  /** @type {Guild[]} User's guilds from /api/guilds */
  guilds: [],
  
  /** @type {string | null} Selected guild ID or 'everyone' */
  selectedGuildId: null,
  
  /** @type {number} Member count of selected guild (for glow threshold) */
  guildMemberCount: 0,
  
  /** @type {User[]} Users visible in current guild context */
  viewableUsers: [],
  
  /** @type {string | null} User selected for individual view */
  selectedUserId: null,
  
  /** @type {Map<string, string[]>} slot key → array of user IDs available */
  heatmapData: new Map(),
  
  /** @type {HTMLElement | null} Currently locked grid cell */
  lockedCell: null,
  
  // ---- Settings Page State ----
  /** @type {Set<string>} Current user's saved availability */
  mySlots: new Set(),
  
  /** @type {Set<string>} Working copy (unsaved changes) */
  pendingSlots: new Set(),
  
  /** @type {boolean} Whether pendingSlots differs from mySlots */
  hasUnsavedChanges: false,
  
  /** @type {User[]} Blocked users from /api/blacklist */
  blacklist: [],
  
  // ---- UI State ----
  /** @type {boolean} Global loading state */
  isLoading: false,
  
  /** @type {string | null} Global error message */
  error: null,
  
  /** @type {boolean} Grid drag in progress */
  isDragging: false,
  
  /** @type {'add' | 'remove' | null} Current drag operation */
  dragMode: null,
  
  /** @type {{ slotIdx: number, dayIdx: number } | null} For paint interpolation */
  lastPaintedCell: null,
  
  /** @type {boolean} Mobile drawer expanded state */
  isDrawerExpanded: false,
};

// ============================================================================
// SUBSCRIPTION SYSTEM
// ============================================================================

/**
 * Map of state key → array of subscriber callbacks.
 * @type {Map<string, Function[]>}
 */
const subscribers = new Map();

/**
 * Callbacks for any state change.
 * @type {Function[]}
 */
const globalSubscribers = [];

/**
 * Subscribe to changes on a specific state key.
 * 
 * @param {keyof state} key - State key to watch
 * @param {Function} callback - Called with (newValue, oldValue, key)
 * @returns {Function} Unsubscribe function
 * 
 * @example
 * const unsub = subscribe('currentUser', (user) => {
 *   console.log('User changed:', user);
 * });
 * // Later: unsub();
 */
export function subscribe(key, callback) {
  if (!subscribers.has(key)) {
    subscribers.set(key, []);
  }
  subscribers.get(key).push(callback);
  
  // Return unsubscribe function
  return () => {
    const subs = subscribers.get(key);
    const idx = subs.indexOf(callback);
    if (idx !== -1) subs.splice(idx, 1);
  };
}

/**
 * Subscribe to any state change.
 * 
 * @param {Function} callback - Called with (key, newValue, oldValue)
 * @returns {Function} Unsubscribe function
 */
export function subscribeAll(callback) {
  globalSubscribers.push(callback);
  
  return () => {
    const idx = globalSubscribers.indexOf(callback);
    if (idx !== -1) globalSubscribers.splice(idx, 1);
  };
}

/**
 * Notify subscribers of a state change.
 * 
 * @param {string} key - State key that changed
 * @param {any} newValue - New value
 * @param {any} oldValue - Previous value
 */
function notify(key, newValue, oldValue) {
  // Key-specific subscribers
  const subs = subscribers.get(key) || [];
  for (const callback of subs) {
    try {
      callback(newValue, oldValue, key);
    } catch (e) {
      console.error(`Error in subscriber for ${key}:`, e);
    }
  }
  
  // Global subscribers
  for (const callback of globalSubscribers) {
    try {
      callback(key, newValue, oldValue);
    } catch (e) {
      console.error('Error in global subscriber:', e);
    }
  }
}

// ============================================================================
// STATE ACCESSORS
// ============================================================================

/**
 * Get the current value of a state key.
 * 
 * @param {keyof state} key - State key
 * @returns {any} Current value
 */
export function getState(key) {
  return state[key];
}

/**
 * Get the entire state object (read-only reference).
 * 
 * @returns {Readonly<typeof state>} State object
 */
export function getFullState() {
  return state;
}

/**
 * Set a state value and notify subscribers.
 * 
 * @param {keyof state} key - State key
 * @param {any} value - New value
 */
export function setState(key, value) {
  const oldValue = state[key];
  
  // Skip if value hasn't changed (shallow comparison)
  if (oldValue === value) return;
  
  state[key] = value;
  notify(key, value, oldValue);
}

/**
 * Update multiple state values at once.
 * 
 * @param {Partial<typeof state>} updates - Object with key-value pairs to update
 */
export function setStates(updates) {
  for (const [key, value] of Object.entries(updates)) {
    setState(key, value);
  }
}

// ============================================================================
// COMPUTED STATE HELPERS
// ============================================================================

/**
 * Check if there are unsaved changes by comparing pendingSlots to mySlots.
 * 
 * Updates the hasUnsavedChanges state automatically.
 */
export function updateUnsavedChangesState() {
  const mySlots = state.mySlots;
  const pendingSlots = state.pendingSlots;
  
  // Compare sets
  let hasChanges = false;
  
  if (mySlots.size !== pendingSlots.size) {
    hasChanges = true;
  } else {
    for (const slot of mySlots) {
      if (!pendingSlots.has(slot)) {
        hasChanges = true;
        break;
      }
    }
  }
  
  setState('hasUnsavedChanges', hasChanges);
}

/**
 * Toggle a slot in pendingSlots.
 * 
 * @param {string} slotKey - Slot key to toggle
 * @returns {boolean} New state (true if now available)
 */
export function togglePendingSlot(slotKey) {
  const pendingSlots = new Set(state.pendingSlots);
  
  if (pendingSlots.has(slotKey)) {
    pendingSlots.delete(slotKey);
  } else {
    pendingSlots.add(slotKey);
  }
  
  setState('pendingSlots', pendingSlots);
  updateUnsavedChangesState();
  
  return pendingSlots.has(slotKey);
}

/**
 * Discard pending changes by resetting pendingSlots to mySlots.
 */
export function discardPendingChanges() {
  setState('pendingSlots', new Set(state.mySlots));
  updateUnsavedChangesState();
}

/**
 * Commit pending changes to mySlots (after successful save).
 */
export function commitPendingChanges() {
  setState('mySlots', new Set(state.pendingSlots));
  updateUnsavedChangesState();
}

/**
 * Initialize pendingSlots from mySlots.
 * 
 * Call this after loading user's availability.
 * 
 * @param {string[]} slots - Array of slot keys
 */
export function initializeSlots(slots) {
  const slotSet = new Set(slots);
  setState('mySlots', slotSet);
  setState('pendingSlots', new Set(slotSet));
  updateUnsavedChangesState();
}

// ============================================================================
// HEATMAP HELPERS
// ============================================================================

/**
 * Build heatmap data from viewable users' availability.
 * 
 * @param {Map<string, string[]>} userAvailability - Map of userId → slot keys
 */
export function buildHeatmapData(userAvailability) {
  const heatmap = new Map();
  
  for (const [userId, slots] of userAvailability) {
    for (const slot of slots) {
      if (!heatmap.has(slot)) {
        heatmap.set(slot, []);
      }
      heatmap.get(slot).push(userId);
    }
  }
  
  setState('heatmapData', heatmap);
}

/**
 * Get the users available at a specific slot.
 * 
 * @param {string} slotKey - Slot key to check
 * @returns {string[]} Array of user IDs available at this slot
 */
export function getUsersAtSlot(slotKey) {
  return state.heatmapData.get(slotKey) || [];
}

// ============================================================================
// RESET
// ============================================================================

/**
 * Reset all state to initial values.
 * 
 * Call this on logout or page transition.
 */
export function resetState() {
  setState('currentUser', null);
  setState('isLoggedIn', false);
  setState('guilds', []);
  setState('selectedGuildId', null);
  setState('guildMemberCount', 0);
  setState('viewableUsers', []);
  setState('selectedUserId', null);
  setState('heatmapData', new Map());
  setState('lockedCell', null);
  setState('mySlots', new Set());
  setState('pendingSlots', new Set());
  setState('hasUnsavedChanges', false);
  setState('blacklist', []);
  setState('isLoading', false);
  setState('error', null);
  setState('isDragging', false);
  setState('dragMode', null);
  setState('lastPaintedCell', null);
  setState('isDrawerExpanded', false);
}
