import PQueue from 'p-queue';

/**
 * Rate limiter for API requests
 */
class RateLimiter {
  constructor(options = {}) {
    this.requestsPerInterval = options.requestsPerInterval || 10;
    this.interval = options.interval || 1000; // ms
    this.concurrency = options.concurrency || 5;

    // Create queue with rate limiting
    this.queue = new PQueue({
      concurrency: this.concurrency,
      interval: this.interval,
      intervalCap: this.requestsPerInterval
    });

    this.stats = {
      total: 0,
      pending: 0,
      completed: 0,
      failed: 0
    };
  }

  /**
   * Add a task to the rate-limited queue
   * @param {function} task - Async function to execute
   * @returns {Promise} Task result
   */
  async add(task) {
    this.stats.total++;
    this.stats.pending++;

    try {
      const result = await this.queue.add(task);
      this.stats.completed++;
      this.stats.pending--;
      return result;
    } catch (error) {
      this.stats.failed++;
      this.stats.pending--;
      throw error;
    }
  }

  /**
   * Wait for all queued tasks to complete
   */
  async onIdle() {
    return this.queue.onIdle();
  }

  /**
   * Get current queue statistics
   */
  getStats() {
    return {
      ...this.stats,
      queueSize: this.queue.size,
      queuePending: this.queue.pending
    };
  }

  /**
   * Clear the queue
   */
  clear() {
    this.queue.clear();
  }

  /**
   * Pause the queue
   */
  pause() {
    this.queue.pause();
  }

  /**
   * Resume the queue
   */
  resume() {
    this.queue.start();
  }
}

/**
 * Create a rate limiter for specific API
 * @param {string} provider - Provider name (itunes, spotify, etc.)
 * @param {object} config - Provider configuration
 */
export function createRateLimiter(provider, config) {
  const rateLimit = config.rateLimit || 10;
  const interval = config.interval || 1000;

  return new RateLimiter({
    requestsPerInterval: rateLimit,
    interval: interval,
    concurrency: config.concurrency || 5
  });
}

export default RateLimiter;
