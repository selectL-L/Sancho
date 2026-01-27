/**
 * @fileoverview Utility functions for the availability web UI.
 * 
 * This module provides date/time formatting, day helpers, and general utilities.
 * All exports use canonical names defined in WEB_REFACTOR_4B_PLAN.md.
 * 
 * @module web-core/utils
 */

// ============================================================================
// CANONICAL CONSTANTS - DO NOT RENAME
// ============================================================================

/** Day keys for slot IDs (Monday = 0) */
export const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];

/** Full day names for display */
export const DAY_LABELS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

/** Abbreviated day names */
export const DAY_LABELS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/** Number of 15-minute slots per hour */
export const SLOTS_PER_HOUR = 4;

/** Hours in a day */
export const HOURS_PER_DAY = 24;

/** Total slots per day (24 × 4 = 96) */
export const SLOTS_PER_DAY = HOURS_PER_DAY * SLOTS_PER_HOUR;

/** Total slots in a week (96 × 7 = 672) */
export const TOTAL_SLOTS = SLOTS_PER_DAY * DAYS.length;

// ============================================================================
// TIME FORMATTING
// ============================================================================

/**
 * Format a time string (HHMM) to human-readable format.
 * 
 * @param {string} time - 4-digit time string like "0900" or "1430"
 * @param {boolean} [includeMinutes=true] - Whether to include minutes in output
 * @returns {string} Formatted time like "9:00 AM" or "9 AM"
 * 
 * @example
 * formatTime("0900") // "9:00 AM"
 * formatTime("1430") // "2:30 PM"
 * formatTime("0000") // "12:00 AM"
 * formatTime("1200", false) // "12 PM"
 */
export function formatTime(time, includeMinutes = true) {
  const hour = parseInt(time.slice(0, 2), 10);
  const minute = time.slice(2);
  const period = hour >= 12 ? 'PM' : 'AM';
  const displayHour = hour === 0 ? 12 : hour > 12 ? hour - 12 : hour;
  
  if (includeMinutes) {
    return `${displayHour}:${minute} ${period}`;
  }
  return `${displayHour} ${period}`;
}

/**
 * Format an hour (0-23) to label format for grid.
 * 
 * @param {number} hour - Hour in 24-hour format (0-23)
 * @returns {string} Formatted hour like "12am", "1pm", "12pm"
 * 
 * @example
 * formatHourLabel(0)  // "12am"
 * formatHourLabel(12) // "12pm"
 * formatHourLabel(13) // "1pm"
 */
export function formatHourLabel(hour) {
  if (hour === 0) return '12am';
  if (hour === 12) return '12pm';
  return hour > 12 ? `${hour - 12}pm` : `${hour}am`;
}

/**
 * Format a time range from two time strings.
 * 
 * @param {string} startTime - Start time in HHMM format
 * @param {string} endTime - End time in HHMM format
 * @returns {string} Formatted range like "9:00 AM - 10:00 AM"
 */
export function formatTimeRange(startTime, endTime) {
  return `${formatTime(startTime)} - ${formatTime(endTime)}`;
}

/**
 * Format a full slot key to human-readable format.
 * 
 * @param {string} slotKey - Slot key like "mon-0900"
 * @returns {string} Formatted string like "Monday at 9:00 AM"
 */
export function formatSlotLabel(slotKey) {
  const [day, time] = slotKey.split('-');
  const dayIndex = DAYS.indexOf(day);
  const dayLabel = dayIndex >= 0 ? DAY_LABELS[dayIndex] : day;
  return `${dayLabel} at ${formatTime(time)}`;
}

// ============================================================================
// DATE HELPERS
// ============================================================================

/**
 * Get the dates for the current week (Monday to Sunday).
 * 
 * @returns {Date[]} Array of 7 Date objects, starting with Monday
 */
export function getWeekDates() {
  const today = new Date();
  const dow = today.getDay(); // 0 = Sunday, 1 = Monday, ...
  const mondayOffset = dow === 0 ? -6 : 1 - dow;
  
  const monday = new Date(today);
  monday.setDate(today.getDate() + mondayOffset);
  
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
}

/**
 * Format a date as DD/MM.
 * 
 * @param {Date} date - Date to format
 * @returns {string} Formatted date like "26/01"
 */
export function formatDateShort(date) {
  const day = String(date.getDate()).padStart(2, '0');
  const month = String(date.getMonth() + 1).padStart(2, '0');
  return `${day}/${month}`;
}

/**
 * Check if a date is today.
 * 
 * @param {Date} date - Date to check
 * @returns {boolean} True if date is today
 */
export function isToday(date) {
  const today = new Date();
  return (
    date.getDate() === today.getDate() &&
    date.getMonth() === today.getMonth() &&
    date.getFullYear() === today.getFullYear()
  );
}

/**
 * Get today's day index (0 = Monday, 6 = Sunday).
 * 
 * @returns {number} Day index 0-6
 */
export function getTodayIndex() {
  const dow = new Date().getDay(); // 0 = Sunday
  return dow === 0 ? 6 : dow - 1;
}

/**
 * Get the current time slot index (0-95).
 * 
 * @returns {number} Slot index based on current time
 */
export function getCurrentSlotIndex() {
  const now = new Date();
  return now.getHours() * SLOTS_PER_HOUR + Math.floor(now.getMinutes() / 15);
}

// ============================================================================
// RELATIVE TIME
// ============================================================================

/**
 * Format an ISO timestamp as relative time.
 * 
 * @param {string} isoString - ISO 8601 timestamp
 * @returns {string} Relative time like "2 hours ago", "just now"
 */
export function formatRelativeTime(isoString) {
  if (!isoString) return 'never';
  
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHour = Math.floor(diffMin / 60);
  const diffDay = Math.floor(diffHour / 24);
  
  if (diffSec < 60) return 'just now';
  if (diffMin < 60) return `${diffMin} minute${diffMin !== 1 ? 's' : ''} ago`;
  if (diffHour < 24) return `${diffHour} hour${diffHour !== 1 ? 's' : ''} ago`;
  if (diffDay < 7) return `${diffDay} day${diffDay !== 1 ? 's' : ''} ago`;
  
  // Older than a week: show date
  return formatDateShort(date);
}

// ============================================================================
// UTILITY FUNCTIONS
// ============================================================================

/**
 * Debounce a function call.
 * 
 * @param {Function} fn - Function to debounce
 * @param {number} ms - Milliseconds to wait
 * @returns {Function} Debounced function
 */
export function debounce(fn, ms) {
  let timeout;
  return function (...args) {
    clearTimeout(timeout);
    timeout = setTimeout(() => fn.apply(this, args), ms);
  };
}

/**
 * Throttle a function call.
 * 
 * @param {Function} fn - Function to throttle
 * @param {number} ms - Minimum milliseconds between calls
 * @returns {Function} Throttled function
 */
export function throttle(fn, ms) {
  let lastCall = 0;
  return function (...args) {
    const now = Date.now();
    if (now - lastCall >= ms) {
      lastCall = now;
      return fn.apply(this, args);
    }
  };
}

/**
 * Clamp a number between min and max.
 * 
 * @param {number} value - Value to clamp
 * @param {number} min - Minimum value
 * @param {number} max - Maximum value
 * @returns {number} Clamped value
 */
export function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}
