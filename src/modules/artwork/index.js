import { join } from 'path';
import { existsSync, mkdirSync } from 'fs';
import config from '../../core/config.js';
import logger from '../../core/logger.js';
import Downloader from '../../core/downloader.js';
import CSVProcessor from '../../core/csv-processor.js';
import { createRateLimiter } from '../../utils/rate-limiter.js';

// Providers
import iTunesProvider from './providers/itunes.js';
import SpotifyProvider from './providers/spotify.js';
import MusicBrainzProvider from './providers/musicbrainz.js';

/**
 * Artwork Module - Main orchestrator
 * Handles provider fallback chain and batch processing
 */
class ArtworkModule {
  constructor() {
    this.config = config.get('artwork');
    this.csvProcessor = new CSVProcessor();
    this.downloader = new Downloader({
      timeout: 30000,
      maxRetries: 3
    });

    // Initialize providers
    this.providers = this.initializeProviders();
    this.rateLimiters = this.initializeRateLimiters();

    // Ensure output directory exists
    this.ensureOutputDirectory();

    // Stats tracking
    this.stats = {
      total: 0,
      succeeded: 0,
      failed: 0,
      byProvider: {}
    };
  }

  /**
   * Initialize all enabled providers
   */
  initializeProviders() {
    const providers = [];
    const providerConfigs = this.config.providers;

    // Priority order: Spotify > MusicBrainz > iTunes
    if (providerConfigs.spotify?.enabled) {
      const spotify = new SpotifyProvider(providerConfigs.spotify);
      if (spotify.enabled) {
        providers.push(spotify);
        logger.info('Spotify provider enabled');
      } else {
        logger.warn('Spotify provider configured but missing credentials');
      }
    }

    if (providerConfigs.musicbrainz?.enabled) {
      providers.push(new MusicBrainzProvider(providerConfigs.musicbrainz));
      logger.info('MusicBrainz provider enabled');
    }

    if (providerConfigs.itunes?.enabled) {
      providers.push(new iTunesProvider(providerConfigs.itunes));
      logger.info('iTunes provider enabled');
    }

    if (providers.length === 0) {
      throw new Error('No artwork providers enabled!');
    }

    return providers;
  }

  /**
   * Initialize rate limiters for each provider
   */
  initializeRateLimiters() {
    const limiters = {};

    for (const provider of this.providers) {
      const providerConfig = this.config.providers[provider.name.toLowerCase()];
      limiters[provider.name] = createRateLimiter(provider.name, providerConfig);
    }

    return limiters;
  }

  /**
   * Ensure output directory exists
   */
  ensureOutputDirectory() {
    if (!existsSync(this.config.outputDir)) {
      mkdirSync(this.config.outputDir, { recursive: true });
    }
  }

  /**
   * Search for artwork using fallback chain
   */
  async searchArtwork(trackData) {
    const { title, artist, isrc, upc, entity_type = 'album' } = trackData;

    logger.debug('Searching artwork', { title, artist, isrc, upc });

    // Try each provider in order until success
    for (const provider of this.providers) {
      try {
        // Check if provider supports available identifiers
        if (isrc && !provider.supportsISRC && upc && !provider.supportsUPC) {
          logger.debug(`Skipping ${provider.name} - doesn't support available identifiers`);
          continue;
        }

        // Search with rate limiting
        const result = await this.rateLimiters[provider.name].add(async () => {
          return await provider.search({
            title,
            artist,
            isrc,
            upc,
            entity: entity_type,
            country: trackData.country || 'us'
          });
        });

        if (result) {
          logger.info(`Artwork found via ${provider.name}`, {
            title,
            artist,
            provider: provider.name
          });

          // Track provider usage
          this.stats.byProvider[provider.name] = (this.stats.byProvider[provider.name] || 0) + 1;

          return {
            ...result,
            provider: provider.name,
            trackData: trackData
          };
        }
      } catch (error) {
        logger.warn(`${provider.name} search failed`, {
          title,
          artist,
          error: error.message
        });
      }
    }

    logger.warn('No artwork found from any provider', { title, artist });
    return null;
  }

  /**
   * Download artwork to file
   */
  async downloadArtwork(artworkData, trackData) {
    const provider = this.providers.find(p => p.name === artworkData.provider);
    const imageUrl = provider ? provider.getBestQualityUrl(artworkData) : artworkData.hires || artworkData.url;

    // Build filename from template
    const filenameTemplate = this.config.fileNameFormat;
    const filename = this.csvProcessor.buildFilename(filenameTemplate, {
      title: trackData.title,
      artist: trackData.artist,
      isrc: trackData.isrc,
      upc: trackData.upc,
      album: artworkData.album || trackData.title
    });

    // Determine file extension from URL
    const extension = this.getFileExtension(imageUrl) || 'jpg';
    const outputPath = join(this.config.outputDir, `${filename}.${extension}`);

    logger.debug(`Downloading: ${imageUrl} -> ${outputPath}`);

    try {
      const result = await this.downloader.download(imageUrl, outputPath, {
        skipExisting: true
      });

      return {
        success: true,
        path: result.path,
        size: result.size,
        skipped: result.skipped,
        url: imageUrl
      };
    } catch (error) {
      logger.error('Download failed', {
        url: imageUrl,
        error: error.message
      });

      return {
        success: false,
        error: error.message,
        url: imageUrl
      };
    }
  }

  /**
   * Get file extension from URL
   */
  getFileExtension(url) {
    try {
      const urlPath = new URL(url).pathname;
      const match = urlPath.match(/\.([a-z0-9]+)$/i);
      return match ? match[1] : null;
    } catch {
      return null;
    }
  }

  /**
   * Process a single track
   */
  async processTrack(trackData) {
    this.stats.total++;

    // Search for artwork
    const artworkData = await this.searchArtwork(trackData);

    if (!artworkData) {
      this.stats.failed++;
      return {
        success: false,
        trackData,
        error: 'No artwork found'
      };
    }

    // Download artwork
    const downloadResult = await this.downloadArtwork(artworkData, trackData);

    if (downloadResult.success) {
      this.stats.succeeded++;
      return {
        success: true,
        trackData,
        artworkData,
        downloadResult
      };
    } else {
      this.stats.failed++;
      return {
        success: false,
        trackData,
        artworkData,
        error: downloadResult.error
      };
    }
  }

  /**
   * Process batch of tracks
   */
  async processBatch(tracks, progressInfo) {
    const results = [];
    const progress = logger.createProgress(tracks.length, 'Artwork batch');

    for (const track of tracks) {
      // Validate track data
      const validation = this.csvProcessor.validateRecord(track, ['title', 'artist']);

      if (!validation.valid) {
        logger.warn('Invalid track data, skipping', {
          track,
          missing: validation.missing
        });
        progress.increment(false);
        results.push({
          success: false,
          trackData: track,
          error: `Missing required fields: ${validation.missing.join(', ')}`
        });
        continue;
      }

      // Process track
      const result = await this.processTrack(track);
      results.push(result);

      progress.increment(result.success);

      // Small delay to avoid overwhelming APIs
      await new Promise(resolve => setTimeout(resolve, 100));
    }

    progress.complete();

    return {
      processed: tracks.length,
      succeeded: results.filter(r => r.success).length,
      failed: results.filter(r => !r.success).length,
      results
    };
  }

  /**
   * Process CSV file
   */
  async processCSV(csvPath) {
    logger.info(`Processing CSV file: ${csvPath}`);

    const allResults = [];

    const stats = await this.csvProcessor.processFile(
      csvPath,
      async (batch, progressInfo) => {
        const batchResult = await this.processBatch(batch, progressInfo);
        allResults.push(...batchResult.results);
        return batchResult;
      }
    );

    // Generate report
    await this.generateReport(allResults, csvPath);

    return {
      ...stats,
      providerStats: this.stats.byProvider
    };
  }

  /**
   * Generate processing report
   */
  async generateReport(results, csvPath) {
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
    const reportFilename = `artwork-report-${timestamp}.csv`;
    const reportPath = join(config.get('logging.outputDir'), reportFilename);

    const reportData = results.map(result => ({
      title: result.trackData.title,
      artist: result.trackData.artist,
      isrc: result.trackData.isrc || '',
      upc: result.trackData.upc || '',
      success: result.success,
      provider: result.artworkData?.provider || '',
      filePath: result.downloadResult?.path || '',
      error: result.error || ''
    }));

    this.csvProcessor.generateReport(reportData, reportPath);
  }

  /**
   * Get current statistics
   */
  getStats() {
    return this.stats;
  }
}

export default ArtworkModule;
