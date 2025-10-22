import { createWriteStream, mkdirSync, existsSync } from 'fs';
import { join } from 'path';
import config from './config.js';

/**
 * Logger utility with file and console output
 */
class Logger {
  constructor() {
    this.logConfig = config.get('logging');
    this.streams = new Map();
    this.initializeLogDirectory();
  }

  initializeLogDirectory() {
    const logDir = this.logConfig.outputDir;
    if (!existsSync(logDir)) {
      mkdirSync(logDir, { recursive: true });
    }
  }

  getLogStream(filename) {
    if (!this.streams.has(filename)) {
      const logPath = join(this.logConfig.outputDir, filename);
      const stream = createWriteStream(logPath, { flags: 'a' });
      this.streams.set(filename, stream);
    }
    return this.streams.get(filename);
  }

  formatMessage(level, message, data = null) {
    const timestamp = new Date().toISOString();
    const dataStr = data ? ` | ${JSON.stringify(data)}` : '';
    return `[${timestamp}] [${level.toUpperCase()}] ${message}${dataStr}`;
  }

  log(level, message, data = null) {
    const logLevels = { error: 0, warn: 1, info: 2, debug: 3 };
    const currentLevel = logLevels[this.logConfig.level] || 2;
    const messageLevel = logLevels[level] || 2;

    if (messageLevel > currentLevel) {
      return;
    }

    const formattedMessage = this.formatMessage(level, message, data);

    // Console output with colors
    if (this.logConfig.console) {
      const colors = {
        error: '\x1b[31m', // red
        warn: '\x1b[33m',  // yellow
        info: '\x1b[36m',  // cyan
        debug: '\x1b[90m', // gray
        reset: '\x1b[0m'
      };
      console.log(`${colors[level] || ''}${formattedMessage}${colors.reset}`);
    }

    // File output
    if (this.logConfig.file) {
      const today = new Date().toISOString().split('T')[0];
      const logFile = `${today}.log`;
      const stream = this.getLogStream(logFile);
      stream.write(formattedMessage + '\n');
    }
  }

  error(message, data) {
    this.log('error', message, data);
  }

  warn(message, data) {
    this.log('warn', message, data);
  }

  info(message, data) {
    this.log('info', message, data);
  }

  debug(message, data) {
    this.log('debug', message, data);
  }

  success(message, data) {
    const timestamp = new Date().toISOString();
    const dataStr = data ? ` | ${JSON.stringify(data)}` : '';
    const formattedMessage = `[${timestamp}] [SUCCESS] ${message}${dataStr}`;

    if (this.logConfig.console) {
      console.log(`\x1b[32m${formattedMessage}\x1b[0m`); // green
    }

    if (this.logConfig.file) {
      const today = new Date().toISOString().split('T')[0];
      const stream = this.getLogStream(`${today}.log`);
      stream.write(formattedMessage + '\n');
    }
  }

  /**
   * Create a progress tracker for batch operations
   */
  createProgress(total, label = 'Processing') {
    let current = 0;
    let succeeded = 0;
    let failed = 0;

    return {
      increment: (success = true) => {
        current++;
        if (success) succeeded++;
        else failed++;

        const percentage = ((current / total) * 100).toFixed(1);
        this.info(`${label}: ${current}/${total} (${percentage}%) | Success: ${succeeded} | Failed: ${failed}`);
      },
      complete: () => {
        this.success(`${label} complete: ${succeeded} succeeded, ${failed} failed out of ${total} total`);
      },
      getStats: () => ({
        total,
        current,
        succeeded,
        failed,
        percentage: ((current / total) * 100).toFixed(1)
      })
    };
  }

  /**
   * Close all log streams
   */
  close() {
    for (const stream of this.streams.values()) {
      stream.end();
    }
    this.streams.clear();
  }
}

// Singleton instance
const logger = new Logger();

export default logger;
