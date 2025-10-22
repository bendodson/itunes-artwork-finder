#!/usr/bin/env node

import { Command } from 'commander';
import { readFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import config from './core/config.js';
import logger from './core/logger.js';
import CSVProcessor from './core/csv-processor.js';

// Modules
import ArtworkModule from './modules/artwork/index.js';
import AudioModule from './modules/audio/index.js';
import LyricsModule from './modules/lyrics/index.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const packageJson = JSON.parse(readFileSync(join(__dirname, '../package.json'), 'utf8'));

/**
 * Music Recovery Tool
 * Automated extraction of artwork, audio, and lyrics from CSV input
 */
class MusicRecoveryTool {
  constructor() {
    this.modules = {
      artwork: new ArtworkModule(),
      audio: new AudioModule(),
      lyrics: new LyricsModule()
    };

    this.csvProcessor = new CSVProcessor();
  }

  /**
   * Run a specific module on a CSV file
   */
  async runModule(moduleName, csvPath) {
    const module = this.modules[moduleName];

    if (!module) {
      logger.error(`Unknown module: ${moduleName}`);
      logger.info(`Available modules: ${Object.keys(this.modules).join(', ')}`);
      process.exit(1);
    }

    logger.info(`Starting ${moduleName} module`);
    logger.info(`CSV file: ${csvPath}`);
    logger.info('─'.repeat(60));

    const startTime = Date.now();

    try {
      const results = await module.processCSV(csvPath);

      const duration = ((Date.now() - startTime) / 1000).toFixed(2);

      logger.info('─'.repeat(60));
      logger.success(`${moduleName} module completed in ${duration}s`);
      logger.info('Results:', results);

      return results;
    } catch (error) {
      logger.error(`${moduleName} module failed`, { error: error.message });
      throw error;
    }
  }

  /**
   * Run all enabled modules on a CSV file
   */
  async runAll(csvPath) {
    logger.info('Running ALL enabled modules');
    logger.info(`CSV file: ${csvPath}`);
    logger.info('═'.repeat(60));

    const results = {};

    // Run artwork module
    if (config.get('artwork.enabled') !== false) {
      results.artwork = await this.runModule('artwork', csvPath);
    }

    // Run audio module if enabled
    if (config.get('audio.enabled')) {
      results.audio = await this.runModule('audio', csvPath);
    }

    // Run lyrics module if enabled
    if (config.get('lyrics.enabled')) {
      results.lyrics = await this.runModule('lyrics', csvPath);
    }

    logger.info('═'.repeat(60));
    logger.success('All modules completed');

    return results;
  }

  /**
   * List available CSV files in input directory
   */
  listCSVFiles() {
    const inputDir = config.get('csv.inputDir');
    const files = this.csvProcessor.findCSVFiles(inputDir);

    if (files.length === 0) {
      logger.warn(`No CSV files found in ${inputDir}`);
      logger.info('Please add CSV files to the data/input directory');
      return;
    }

    logger.info(`Found ${files.length} CSV file(s):`);
    files.forEach((file, index) => {
      console.log(`  ${index + 1}. ${file}`);
    });
  }
}

// CLI Program
const program = new Command();

program
  .name('music-recovery-tool')
  .description('Automated music metadata recovery tool - artwork, audio, and lyrics extraction')
  .version(packageJson.version);

program
  .command('artwork <csvFile>')
  .description('Extract and download artwork for tracks in CSV file')
  .action(async (csvFile) => {
    const tool = new MusicRecoveryTool();
    await tool.runModule('artwork', csvFile);
  });

program
  .command('audio <csvFile>')
  .description('Extract audio files for tracks in CSV file (not yet implemented)')
  .action(async (csvFile) => {
    const tool = new MusicRecoveryTool();
    await tool.runModule('audio', csvFile);
  });

program
  .command('lyrics <csvFile>')
  .description('Extract lyrics for tracks in CSV file (not yet implemented)')
  .action(async (csvFile) => {
    const tool = new MusicRecoveryTool();
    await tool.runModule('lyrics', csvFile);
  });

program
  .command('all <csvFile>')
  .description('Run all enabled modules on CSV file')
  .action(async (csvFile) => {
    const tool = new MusicRecoveryTool();
    await tool.runAll(csvFile);
  });

program
  .command('list')
  .description('List available CSV files in input directory')
  .action(() => {
    const tool = new MusicRecoveryTool();
    tool.listCSVFiles();
  });

// Parse CLI arguments
program.parse(process.argv);

// Show help if no arguments
if (!process.argv.slice(2).length) {
  program.outputHelp();
}

// Handle errors
process.on('unhandledRejection', (error) => {
  logger.error('Unhandled error', { error: error.message });
  process.exit(1);
});

export default MusicRecoveryTool;
