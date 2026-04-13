/**
 * @fileoverview Grid logic for the availability web UI.
 * 
 * This module provides pure functions for grid operations:
 * - Slot generation and parsing
 * - Drag selection calculation
 * - Heatmap calculation
 * - Resolution tier merging (for mobile)
 * 
 * ⚠️ THE GRID IS HARD. DO NOT UNDERESTIMATE IT.
 * 
 * The grid handles 672 cells (7 days × 96 time slots), drag painting with
 * Bresenham line interpolation, heatmap rendering with intensity levels,
 * and mobile resolution tiers. Follow the patterns from the working
 * desktop implementation.
 * 
 * @module web-core/grid
 */

import { 
  DAYS, 
  SLOTS_PER_HOUR, 
  HOURS_PER_DAY, 
  SLOTS_PER_DAY,
  formatHourLabel 
} from './utils.js';

// Re-export constants for convenience
export { DAYS, SLOTS_PER_HOUR, HOURS_PER_DAY, SLOTS_PER_DAY };

// ============================================================================
// SLOT GENERATION
// ============================================================================

/**
 * Generate all time slot keys for a single day.
 * 
 * Returns 96 slot objects (24 hours × 4 slots per hour).
 * 
 * @returns {TimeSlot[]} Array of time slot objects
 * 
 * @typedef {Object} TimeSlot
 * @property {string} key - Time key like "0900", "1430"
 * @property {number} hour - Hour 0-23
 * @property {number} minute - Minute 0, 15, 30, or 45
 * @property {string} label - Display label (only for hour marks)
 * @property {boolean} isHour - True if this is an hour mark (minute === 0)
 * @property {boolean} isHalfHour - True if minute === 30
 */
export function generateTimeSlots() {
  const slots = [];
  
  for (let hour = 0; hour < HOURS_PER_DAY; hour++) {
    for (let minute = 0; minute < 60; minute += 15) {
      const key = `${String(hour).padStart(2, '0')}${String(minute).padStart(2, '0')}`;
      const isHour = minute === 0;
      const isHalfHour = minute === 30;
      
      slots.push({
        key,
        hour,
        minute,
        label: isHour ? formatHourLabel(hour) : '',
        isHour,
        isHalfHour,
      });
    }
  }
  
  return slots;
}

/**
 * Generate all slot keys for the entire week.
 * 
 * Returns 672 slot keys (7 days × 96 time slots).
 * 
 * @returns {string[]} Array of slot keys like "mon-0900"
 */
export function generateAllSlotKeys() {
  const timeSlots = generateTimeSlots();
  const allKeys = [];
  
  for (const day of DAYS) {
    for (const slot of timeSlots) {
      allKeys.push(`${day}-${slot.key}`);
    }
  }
  
  return allKeys;
}

// ============================================================================
// SLOT PARSING
// ============================================================================

/**
 * Parse a slot key into its components.
 * 
 * @param {string} slotKey - Slot key like "mon-0900"
 * @returns {ParsedSlot | null} Parsed components or null if invalid
 * 
 * @typedef {Object} ParsedSlot
 * @property {string} day - Day key ('mon', 'tue', etc.)
 * @property {number} dayIdx - Day index 0-6
 * @property {string} time - Time key like "0900"
 * @property {number} hour - Hour 0-23
 * @property {number} minute - Minute 0, 15, 30, or 45
 * @property {number} slotIdx - Time slot index 0-95
 */
export function parseSlot(slotKey) {
  const parts = slotKey.split('-');
  if (parts.length !== 2) return null;
  
  const [day, time] = parts;
  const dayIdx = DAYS.indexOf(day);
  if (dayIdx === -1) return null;
  
  if (time.length !== 4) return null;
  const hour = parseInt(time.slice(0, 2), 10);
  const minute = parseInt(time.slice(2), 10);
  
  if (isNaN(hour) || isNaN(minute)) return null;
  if (hour < 0 || hour > 23) return null;
  if (![0, 15, 30, 45].includes(minute)) return null;
  
  const slotIdx = hour * SLOTS_PER_HOUR + Math.floor(minute / 15);
  
  return { day, dayIdx, time, hour, minute, slotIdx };
}

/**
 * Create a slot key from components.
 * 
 * @param {string} day - Day key ('mon', 'tue', etc.)
 * @param {number} hour - Hour 0-23
 * @param {number} minute - Minute 0, 15, 30, or 45
 * @returns {string} Slot key like "mon-0900"
 */
export function formatSlotKey(day, hour, minute) {
  const time = `${String(hour).padStart(2, '0')}${String(minute).padStart(2, '0')}`;
  return `${day}-${time}`;
}

/**
 * Create a slot key from indices.
 * 
 * @param {number} dayIdx - Day index 0-6
 * @param {number} slotIdx - Time slot index 0-95
 * @returns {string} Slot key like "mon-0900"
 */
export function slotKeyFromIndices(dayIdx, slotIdx) {
  const day = DAYS[dayIdx];
  const hour = Math.floor(slotIdx / SLOTS_PER_HOUR);
  const minute = (slotIdx % SLOTS_PER_HOUR) * 15;
  return formatSlotKey(day, hour, minute);
}

/**
 * Get indices from a slot key.
 * 
 * @param {string} slotKey - Slot key like "mon-0900"
 * @returns {{ dayIdx: number, slotIdx: number } | null} Indices or null
 */
export function indiciesFromSlotKey(slotKey) {
  const parsed = parseSlot(slotKey);
  if (!parsed) return null;
  return { dayIdx: parsed.dayIdx, slotIdx: parsed.slotIdx };
}

// ============================================================================
// DRAG SELECTION
// ============================================================================

/**
 * Calculate all slots in a rectangular drag selection.
 * 
 * Uses Bresenham's line algorithm to interpolate between start and end,
 * ensuring no cells are skipped during fast drags.
 * 
 * @param {number} startSlotIdx - Starting time slot index (0-95)
 * @param {number} startDayIdx - Starting day index (0-6)
 * @param {number} endSlotIdx - Ending time slot index (0-95)
 * @param {number} endDayIdx - Ending day index (0-6)
 * @returns {Array<{ dayIdx: number, slotIdx: number }>} Array of cell coordinates
 */
export function interpolateDragPath(startSlotIdx, startDayIdx, endSlotIdx, endDayIdx) {
  const cells = [];
  
  // Bresenham's line algorithm
  const dSlot = Math.abs(endSlotIdx - startSlotIdx);
  const dDay = Math.abs(endDayIdx - startDayIdx);
  const sSlot = startSlotIdx < endSlotIdx ? 1 : -1;
  const sDay = startDayIdx < endDayIdx ? 1 : -1;
  
  let err = dSlot - dDay;
  let curSlot = startSlotIdx;
  let curDay = startDayIdx;
  
  while (true) {
    cells.push({ dayIdx: curDay, slotIdx: curSlot });
    
    if (curSlot === endSlotIdx && curDay === endDayIdx) break;
    
    const e2 = 2 * err;
    if (e2 > -dDay) {
      err -= dDay;
      curSlot += sSlot;
    }
    if (e2 < dSlot) {
      err += dSlot;
      curDay += sDay;
    }
  }
  
  return cells;
}

/**
 * Expand a rectangular selection to all contained slots.
 * 
 * Given two corner cells, returns all slots within the rectangle.
 * This is for "box select" mode, not line interpolation.
 * 
 * @param {number} startSlotIdx - First corner time slot index
 * @param {number} startDayIdx - First corner day index
 * @param {number} endSlotIdx - Second corner time slot index
 * @param {number} endDayIdx - Second corner day index
 * @returns {string[]} Array of slot keys in the rectangle
 */
export function expandRectSelection(startSlotIdx, startDayIdx, endSlotIdx, endDayIdx) {
  const minSlot = Math.min(startSlotIdx, endSlotIdx);
  const maxSlot = Math.max(startSlotIdx, endSlotIdx);
  const minDay = Math.min(startDayIdx, endDayIdx);
  const maxDay = Math.max(startDayIdx, endDayIdx);
  
  const slots = [];
  
  for (let dayIdx = minDay; dayIdx <= maxDay; dayIdx++) {
    for (let slotIdx = minSlot; slotIdx <= maxSlot; slotIdx++) {
      slots.push(slotKeyFromIndices(dayIdx, slotIdx));
    }
  }
  
  return slots;
}

// ============================================================================
// HEATMAP CALCULATION
// ============================================================================

/**
 * Calculate heatmap data from user availability.
 * 
 * Takes a map of user ID → slots and produces a map of slot → user IDs.
 * 
 * @param {Map<string, string[]>} userSlots - Map of userId to their slot keys
 * @returns {Map<string, string[]>} Map of slotKey to array of userIds available
 */
export function calculateHeatmap(userSlots) {
  const heatmap = new Map();
  
  for (const [userId, slots] of userSlots) {
    for (const slot of slots) {
      if (!heatmap.has(slot)) {
        heatmap.set(slot, []);
      }
      heatmap.get(slot).push(userId);
    }
  }
  
  return heatmap;
}

/**
 * Calculate cell alpha (opacity) for heatmap display.
 * 
 * Uses a smooth gradient from 0.15 to 0.92 based on the fraction
 * of participants available.
 * 
 * @param {number} count - Number of users available at this slot
 * @param {number} total - Total number of participants
 * @returns {number} Alpha value 0-0.92
 */
export function calculateCellAlpha(count, total) {
  if (count === 0) return 0;
  if (total <= 1) return 0.53; // Single person: fixed alpha
  
  const fraction = count / total;
  // Scale from 0.15 (min visible) to 0.92 (max)
  return 0.15 + fraction * (0.92 - 0.15);
}

/**
 * Calculate glow intensity for high-availability slots.
 * 
 * Glow is disabled if less than 10% of guild members are participating.
 * 
 * @param {number} count - Number of users available at this slot
 * @param {number} total - Total number of participants
 * @param {number} memberCount - Total guild member count (for threshold)
 * @returns {number} Glow intensity 0-1
 */
export function calculateGlowIntensity(count, total, memberCount = 0) {
  if (total === 0 || count === 0) return 0;
  
  // Disable glow if less than 10% of guild members are participating
  if (memberCount > 0 && total < memberCount * 0.1) return 0;
  
  // Calculate "good enough" threshold based on group size
  const goodEnough = total <= 7 ? 1.0 : Math.max(0.4, 1.0 - (total - 7) * 0.05);
  const target = Math.ceil(total * goodEnough);
  
  // Quadratic curve for glow intensity
  return Math.min(1, Math.pow(count / target, 2.5));
}

// ============================================================================
// RESOLUTION TIERS (MOBILE)
// ============================================================================

/**
 * Resolution tier configurations for mobile zoom levels.
 * 
 * - '7d': Show all 7 days with 30-minute blocks (merged from 15-min)
 * - '3d': Show 3 days with 15-minute blocks (native resolution)
 * - '1d': Show 1 day with 15-minute blocks (native resolution)
 */
export const RESOLUTION_TIERS = {
  '7d': { 
    days: 7, 
    slotsPerHour: 2, 
    label: '30 min',
    mergeSize: 2  // Merge every 2 slots into 1
  },
  '3d': { 
    days: 3, 
    slotsPerHour: 4, 
    label: '15 min',
    mergeSize: 1  // No merging
  },
  '1d': { 
    days: 1, 
    slotsPerHour: 4, 
    label: '15 min',
    mergeSize: 1  // No merging
  },
};

/**
 * @typedef {Object} MergedSlot
 * @property {string} displayTime - Display string like "9:00 - 9:30"
 * @property {string[]} sourceSlots - Original slot keys that were merged
 * @property {number} startHour - Starting hour
 * @property {number} startMinute - Starting minute
 * @property {number} endHour - Ending hour
 * @property {number} endMinute - Ending minute
 */

/**
 * Merge time slots for lower resolution display.
 * 
 * For 7-day view, merges adjacent 15-minute slots into 30-minute blocks.
 * 
 * @param {string} day - Day key like 'mon'
 * @param {keyof RESOLUTION_TIERS} tierKey - Resolution tier
 * @returns {MergedSlot[]} Array of merged slot definitions
 */
export function getMergedTimeSlotsForDay(day, tierKey) {
  const tier = RESOLUTION_TIERS[tierKey];
  if (!tier || tier.mergeSize === 1) {
    // No merging needed - return native slots
    const timeSlots = generateTimeSlots();
    return timeSlots.map(slot => ({
      displayTime: slot.label || `${slot.hour}:${String(slot.minute).padStart(2, '0')}`,
      sourceSlots: [`${day}-${slot.key}`],
      startHour: slot.hour,
      startMinute: slot.minute,
      endHour: slot.minute === 45 ? slot.hour + 1 : slot.hour,
      endMinute: (slot.minute + 15) % 60,
    }));
  }
  
  // Merge slots
  const merged = [];
  const timeSlots = generateTimeSlots();
  
  for (let i = 0; i < timeSlots.length; i += tier.mergeSize) {
    const startSlot = timeSlots[i];
    const endSlot = timeSlots[Math.min(i + tier.mergeSize - 1, timeSlots.length - 1)];
    
    // Calculate end time (15 minutes after the last slot in the merge)
    let endMinute = endSlot.minute + 15;
    let endHour = endSlot.hour;
    if (endMinute >= 60) {
      endMinute = 0;
      endHour = (endHour + 1) % 24;
    }
    
    const sourceSlots = [];
    for (let j = i; j < Math.min(i + tier.mergeSize, timeSlots.length); j++) {
      sourceSlots.push(`${day}-${timeSlots[j].key}`);
    }
    
    merged.push({
      displayTime: `${startSlot.label || formatHourLabel(startSlot.hour)}`,
      sourceSlots,
      startHour: startSlot.hour,
      startMinute: startSlot.minute,
      endHour,
      endMinute,
    });
  }
  
  return merged;
}

/**
 * Check if any source slot in a merged block is available.
 * 
 * @param {MergedSlot} mergedSlot - Merged slot definition
 * @param {Set<string>} availableSlots - Set of available slot keys
 * @returns {boolean} True if any source slot is available
 */
export function isMergedSlotAvailable(mergedSlot, availableSlots) {
  return mergedSlot.sourceSlots.some(slot => availableSlots.has(slot));
}

/**
 * Get heatmap count for a merged slot (sum of all source slots).
 * 
 * @param {MergedSlot} mergedSlot - Merged slot definition
 * @param {Map<string, string[]>} heatmapData - Heatmap data
 * @returns {number} Maximum count from any source slot
 */
export function getMergedSlotHeatmapCount(mergedSlot, heatmapData) {
  let maxCount = 0;
  for (const slot of mergedSlot.sourceSlots) {
    const users = heatmapData.get(slot) || [];
    maxCount = Math.max(maxCount, users.length);
  }
  return maxCount;
}
