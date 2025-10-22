import { createReadStream, readdirSync } from 'fs';
import { parse } from 'csv-parse';
import { stringify } from 'csv-stringify/sync';
import { writeFileSync } from 'fs';
import { join } from 'path';
import config from './config.js';
import logger from './logger.js';

/**
 * CSV file processor with streaming and batch support
 */
class CSVProcessor {
  constructor(options = {}) {
    this.csvConfig = config.get('csv');
    this.batchSize = options.batchSize || this.csvConfig.batchSize || 50;
    this.delimiter = options.delimiter || this.csvConfig.delimiter || ',';
    this.encoding = options.encoding || this.csvConfig.encoding || 'utf8';
  }

  /**
   * Parse CSV file and process in batches
   * @param {string} filePath - Path to CSV file
   * @param {function} batchHandler - Async function to process each batch
   * @returns {Promise<object>} Processing statistics
   */
  async processFile(filePath, batchHandler) {
    return new Promise((resolve, reject) => {
      const records = [];
      const stats = {
        total: 0,
        processed: 0,
        succeeded: 0,
        failed: 0,
        startTime: Date.now()
      };

      logger.info(`Starting CSV processing: ${filePath}`);

      const parser = parse({
        delimiter: this.delimiter,
        columns: true,
        skip_empty_lines: true,
        trim: true,
        cast: true
      });

      const stream = createReadStream(filePath, { encoding: this.encoding });

      stream.pipe(parser)
        .on('data', (record) => {
          records.push(record);
          stats.total++;

          // Process batch when it reaches the batch size
          if (records.length >= this.batchSize) {
            parser.pause();
            const batch = records.splice(0, this.batchSize);

            batchHandler(batch, stats)
              .then((result) => {
                stats.processed += result.processed || 0;
                stats.succeeded += result.succeeded || 0;
                stats.failed += result.failed || 0;
                parser.resume();
              })
              .catch((error) => {
                logger.error('Batch processing error', { error: error.message });
                stats.failed += batch.length;
                parser.resume();
              });
          }
        })
        .on('end', async () => {
          // Process remaining records
          if (records.length > 0) {
            try {
              const result = await batchHandler(records, stats);
              stats.processed += result.processed || 0;
              stats.succeeded += result.succeeded || 0;
              stats.failed += result.failed || 0;
            } catch (error) {
              logger.error('Final batch processing error', { error: error.message });
              stats.failed += records.length;
            }
          }

          stats.endTime = Date.now();
          stats.duration = stats.endTime - stats.startTime;

          logger.success('CSV processing complete', {
            total: stats.total,
            succeeded: stats.succeeded,
            failed: stats.failed,
            duration: `${(stats.duration / 1000).toFixed(2)}s`
          });

          resolve(stats);
        })
        .on('error', (error) => {
          logger.error('CSV parsing error', { error: error.message });
          reject(error);
        });
    });
  }

  /**
   * Read all CSV files from a directory
   * @param {string} dirPath - Directory path
   * @returns {Array<string>} Array of CSV file paths
   */
  findCSVFiles(dirPath) {
    const files = readdirSync(dirPath);
    return files
      .filter(file => file.toLowerCase().endsWith('.csv'))
      .map(file => join(dirPath, file));
  }

  /**
   * Validate CSV record has required fields
   * @param {object} record - CSV record
   * @param {Array<string>} requiredFields - Required field names
   * @returns {object} Validation result
   */
  validateRecord(record, requiredFields = ['title', 'artist']) {
    const missing = [];

    for (const field of requiredFields) {
      if (!record[field] || record[field].toString().trim() === '') {
        missing.push(field);
      }
    }

    return {
      valid: missing.length === 0,
      missing,
      record
    };
  }

  /**
   * Generate a results report CSV
   * @param {Array} results - Array of result objects
   * @param {string} outputPath - Output file path
   */
  generateReport(results, outputPath) {
    try {
      const csv = stringify(results, {
        header: true,
        columns: Object.keys(results[0] || {})
      });

      writeFileSync(outputPath, csv, 'utf8');
      logger.success(`Report generated: ${outputPath}`);
      return true;
    } catch (error) {
      logger.error('Failed to generate report', { error: error.message });
      return false;
    }
  }

  /**
   * Sanitize filename from track metadata
   * @param {string} text - Input text
   * @returns {string} Sanitized filename
   */
  sanitizeFilename(text) {
    if (!text) return 'unknown';

    return text
      .replace(/[<>:"/\\|?*\x00-\x1F]/g, '') // Remove invalid chars
      .replace(/\s+/g, ' ') // Normalize spaces
      .trim()
      .substring(0, 200); // Limit length
  }

  /**
   * Build filename from template and data
   * @param {string} template - Filename template
   * @param {object} data - Track data
   * @returns {string} Generated filename
   */
  buildFilename(template, data) {
    let filename = template;

    const replacements = {
      '{title}': this.sanitizeFilename(data.title),
      '{artist}': this.sanitizeFilename(data.artist),
      '{isrc}': this.sanitizeFilename(data.isrc),
      '{upc}': this.sanitizeFilename(data.upc),
      '{album}': this.sanitizeFilename(data.album || data.title)
    };

    for (const [key, value] of Object.entries(replacements)) {
      filename = filename.replace(key, value || 'unknown');
    }

    return this.sanitizeFilename(filename);
  }
}

export default CSVProcessor;
