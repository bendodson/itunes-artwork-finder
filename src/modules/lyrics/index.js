import logger from '../../core/logger.js';

/**
 * Lyrics Module - Placeholder for future implementation
 *
 * This module will handle lyrics extraction from legal APIs.
 *
 * Recommended APIs:
 * 1. Musixmatch API (https://developer.musixmatch.com/)
 * 2. Genius API (https://docs.genius.com/)
 * 3. LyricFind API (commercial)
 * 4. ChartLyrics API (free, limited)
 *
 * All require API keys and have usage limits.
 */
class LyricsModule {
  constructor() {
    logger.info('Lyrics module initialized (not yet implemented)');
  }

  async processCSV(csvPath) {
    logger.warn('Lyrics module not yet implemented');
    logger.info('To extract lyrics, you can use:');
    logger.info('1. Musixmatch API (requires free API key)');
    logger.info('2. Genius API (requires free API key)');
    logger.info('3. LyricFind API (commercial)');

    return {
      total: 0,
      succeeded: 0,
      failed: 0,
      message: 'Lyrics module not implemented'
    };
  }
}

export default LyricsModule;
