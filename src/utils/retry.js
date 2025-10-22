import pRetry from 'p-retry';
import logger from '../core/logger.js';

/**
 * Retry wrapper with exponential backoff
 * @param {function} fn - Function to retry
 * @param {object} options - Retry options
 * @returns {Promise} Function result
 */
export async function retry(fn, options = {}) {
  const {
    retries = 3,
    minTimeout = 1000,
    maxTimeout = 10000,
    factor = 2,
    onFailedAttempt = null,
    ...restOptions
  } = options;

  return pRetry(fn, {
    retries,
    minTimeout,
    maxTimeout,
    factor,
    onFailedAttempt: (error) => {
      logger.warn(`Retry attempt ${error.attemptNumber} failed`, {
        retriesLeft: error.retriesLeft,
        error: error.message
      });

      if (onFailedAttempt) {
        onFailedAttempt(error);
      }
    },
    ...restOptions
  });
}

/**
 * Check if error is retryable
 * @param {Error} error - Error object
 * @returns {boolean} Whether error should be retried
 */
export function isRetryableError(error) {
  // Network errors
  if (error.code === 'ECONNRESET' ||
      error.code === 'ETIMEDOUT' ||
      error.code === 'ENOTFOUND' ||
      error.code === 'ECONNREFUSED') {
    return true;
  }

  // HTTP status codes that should be retried
  if (error.response) {
    const status = error.response.status;
    return status === 429 || // Too many requests
           status === 500 || // Internal server error
           status === 502 || // Bad gateway
           status === 503 || // Service unavailable
           status === 504;   // Gateway timeout
  }

  return false;
}

/**
 * Create a retry wrapper with predefined options
 * @param {object} defaultOptions - Default retry options
 * @returns {function} Retry function with preset options
 */
export function createRetryWrapper(defaultOptions = {}) {
  return (fn, options = {}) => {
    return retry(fn, { ...defaultOptions, ...options });
  };
}

export default retry;
