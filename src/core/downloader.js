import { createWriteStream, existsSync, mkdirSync } from 'fs';
import { dirname } from 'path';
import axios from 'axios';
import logger from './logger.js';

/**
 * Generic file downloader with progress tracking
 */
class Downloader {
  constructor(options = {}) {
    this.timeout = options.timeout || 30000;
    this.maxRetries = options.maxRetries || 3;
    this.retryDelay = options.retryDelay || 2000;
  }

  /**
   * Download a file from URL to local path
   * @param {string} url - Source URL
   * @param {string} outputPath - Destination file path
   * @param {object} options - Additional options
   * @returns {Promise<object>} Download result
   */
  async download(url, outputPath, options = {}) {
    const maxRetries = options.maxRetries || this.maxRetries;

    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        return await this._downloadAttempt(url, outputPath, options);
      } catch (error) {
        if (attempt === maxRetries) {
          throw new Error(`Failed to download after ${maxRetries} attempts: ${error.message}`);
        }

        const delay = this.retryDelay * Math.pow(2, attempt - 1); // Exponential backoff
        logger.warn(`Download attempt ${attempt} failed, retrying in ${delay}ms`, {
          url,
          error: error.message
        });

        await this.sleep(delay);
      }
    }
  }

  async _downloadAttempt(url, outputPath, options = {}) {
    // Ensure directory exists
    const dir = dirname(outputPath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }

    // Check if file already exists and skip if configured
    if (options.skipExisting && existsSync(outputPath)) {
      logger.debug(`File already exists, skipping: ${outputPath}`);
      return {
        success: true,
        path: outputPath,
        skipped: true,
        size: 0
      };
    }

    const response = await axios({
      method: 'GET',
      url: url,
      responseType: 'stream',
      timeout: options.timeout || this.timeout,
      headers: options.headers || {},
      maxRedirects: 5
    });

    return new Promise((resolve, reject) => {
      const writer = createWriteStream(outputPath);
      let downloadedBytes = 0;
      const totalBytes = parseInt(response.headers['content-length'] || '0', 10);

      response.data.on('data', (chunk) => {
        downloadedBytes += chunk.length;
        if (options.onProgress && totalBytes > 0) {
          options.onProgress({
            downloaded: downloadedBytes,
            total: totalBytes,
            percentage: ((downloadedBytes / totalBytes) * 100).toFixed(2)
          });
        }
      });

      response.data.pipe(writer);

      writer.on('finish', () => {
        resolve({
          success: true,
          path: outputPath,
          size: downloadedBytes,
          skipped: false
        });
      });

      writer.on('error', (error) => {
        reject(new Error(`Write error: ${error.message}`));
      });

      response.data.on('error', (error) => {
        reject(new Error(`Stream error: ${error.message}`));
      });
    });
  }

  /**
   * Download multiple files with concurrency control
   * @param {Array} downloads - Array of {url, path} objects
   * @param {object} options - Options including concurrency limit
   * @returns {Promise<Array>} Results array
   */
  async downloadBatch(downloads, options = {}) {
    const concurrency = options.concurrency || 5;
    const results = [];
    const queue = [...downloads];

    const workers = Array(concurrency).fill(null).map(async () => {
      while (queue.length > 0) {
        const download = queue.shift();
        if (!download) break;

        try {
          const result = await this.download(download.url, download.path, {
            ...options,
            metadata: download.metadata
          });
          results.push({
            ...result,
            url: download.url,
            metadata: download.metadata
          });
        } catch (error) {
          results.push({
            success: false,
            url: download.url,
            path: download.path,
            error: error.message,
            metadata: download.metadata
          });
        }
      }
    });

    await Promise.all(workers);
    return results;
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

export default Downloader;
