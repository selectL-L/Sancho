/**
 * @fileoverview Main entry point for web-core modules.
 * 
 * Re-exports all modules for convenient importing:
 * 
 * @example
 * // Import everything
 * import * as core from '/web-core/index.js';
 * 
 * // Or import specific modules
 * import { api, state, grid, theme, utils } from '/web-core/index.js';
 * 
 * @module web-core
 */

// Re-export all modules as namespaces
export * as api from './api.js';
export * as state from './state.js';
export * as grid from './grid.js';
export * as theme from './theme.js';
export * as utils from './utils.js';

// Also export commonly used items directly for convenience
export { 
  // Utils constants
  DAYS, 
  DAY_LABELS, 
  DAY_LABELS_SHORT,
  SLOTS_PER_HOUR,
  HOURS_PER_DAY,
  SLOTS_PER_DAY,
  TOTAL_SLOTS,
  // Utils functions
  formatTime,
  formatHourLabel,
  formatSlotLabel,
  formatRelativeTime,
  getWeekDates,
  isToday,
  getTodayIndex,
  getCurrentSlotIndex,
  debounce,
  throttle,
} from './utils.js';

export {
  // Theme
  getPreferredTheme,
  applyTheme,
  toggleTheme,
  initTheme,
  setThemeColor,
  getThemeColorRGB,
} from './theme.js';

export {
  // API
  getMe,
  getConfig,
  getGuilds,
  getMyAvailability,
  getUserAvailability,
  saveAvailability,
  clearAvailability,
  getViewableUsers,
  getAllViewableUsers,
  getBlacklist,
  blockUser,
  unblockUser,
  resolveUser,
  deleteAccount,
  login,
  logout,
} from './api.js';

export {
  // State
  subscribe,
  subscribeAll,
  getState,
  getFullState,
  setState,
  setStates,
  togglePendingSlot,
  discardPendingChanges,
  commitPendingChanges,
  initializeSlots,
  buildHeatmapData,
  getUsersAtSlot,
  resetState,
} from './state.js';

export {
  // Grid
  generateTimeSlots,
  generateAllSlotKeys,
  parseSlot,
  formatSlotKey,
  slotKeyFromIndices,
  indiciesFromSlotKey,
  interpolateDragPath,
  expandRectSelection,
  calculateHeatmap,
  calculateCellAlpha,
  calculateGlowIntensity,
  RESOLUTION_TIERS,
  getMergedTimeSlotsForDay,
  isMergedSlotAvailable,
  getMergedSlotHeatmapCount,
} from './grid.js';
