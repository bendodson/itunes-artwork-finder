import axios from 'axios';
import logger from '../../../core/logger.js';
import { retry, isRetryableError } from '../../../utils/retry.js';

/**
 * MusicBrainz + Cover Art Archive Provider
 * Supports ISRC and UPC lookups
 */
class MusicBrainzProvider {
  constructor(config = {}) {
    this.name = 'MusicBrainz';
    this.baseUrl = config.baseUrl || 'https://musicbrainz.org/ws/2';
    this.coverArtUrl = config.coverArtUrl || 'https://coverartarchive.org';
    this.timeout = config.timeout || 10000;
    this.enabled = config.enabled !== false;
    this.userAgent = config.userAgent || 'MusicRecoveryTool/1.0.0';

    // Support capabilities
    this.supportsISRC = true;
    this.supportsUPC = true;

    // Rate limiting: MusicBrainz requires 1 req/sec
    this.lastRequestTime = 0;
    this.minRequestInterval = 1000; // ms
  }

  /**
   * Respect rate limit (1 request per second)
   */
  async rateLimit() {
    const now = Date.now();
    const timeSinceLastRequest = now - this.lastRequestTime;

    if (timeSinceLastRequest < this.minRequestInterval) {
      const waitTime = this.minRequestInterval - timeSinceLastRequest;
      await new Promise(resolve => setTimeout(resolve, waitTime));
    }

    this.lastRequestTime = Date.now();
  }

  /**
   * Search for artwork using various parameters
   */
  async search(params) {
    const { isrc, upc, title, artist } = params;

    try {
      // Try ISRC first (most accurate)
      if (isrc) {
        const result = await this.searchByISRC(isrc);
        if (result) return result;
      }

      // Try UPC
      if (upc) {
        const result = await this.searchByUPC(upc);
        if (result) return result;
      }

      // Fallback to text search
      if (title && artist) {
        return await this.searchByText(title, artist);
      }

      return null;
    } catch (error) {
      logger.error('MusicBrainz search failed', {
        params,
        error: error.message
      });
      return null;
    }
  }

  /**
   * Search by ISRC
   */
  async searchByISRC(isrc) {
    await this.rateLimit();

    logger.debug(`MusicBrainz ISRC lookup: ${isrc}`);

    const url = `${this.baseUrl}/recording`;

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          query: `isrc:${isrc}`,
          fmt: 'json'
        },
        headers: {
          'User-Agent': this.userAgent
        },
        timeout: this.timeout
      });
    }, {
      retries: 2,
      shouldRetry: isRetryableError
    });

    if (response.data.recordings && response.data.recordings.length > 0) {
      const recording = response.data.recordings[0];
      return await this.getArtworkFromRecording(recording);
    }

    return null;
  }

  /**
   * Search by UPC/Barcode
   */
  async searchByUPC(upc) {
    await this.rateLimit();

    logger.debug(`MusicBrainz UPC lookup: ${upc}`);

    const url = `${this.baseUrl}/release`;

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          query: `barcode:${upc}`,
          fmt: 'json'
        },
        headers: {
          'User-Agent': this.userAgent
        },
        timeout: this.timeout
      });
    }, {
      retries: 2,
      shouldRetry: isRetryableError
    });

    if (response.data.releases && response.data.releases.length > 0) {
      const release = response.data.releases[0];
      return await this.getArtworkFromRelease(release);
    }

    return null;
  }

  /**
   * Search by artist and title
   */
  async searchByText(title, artist) {
    await this.rateLimit();

    logger.debug(`MusicBrainz text search: ${artist} - ${title}`);

    const url = `${this.baseUrl}/recording`;

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          query: `recording:"${title}" AND artist:"${artist}"`,
          fmt: 'json',
          limit: 5
        },
        headers: {
          'User-Agent': this.userAgent
        },
        timeout: this.timeout
      });
    }, {
      retries: 2,
      shouldRetry: isRetryableError
    });

    if (response.data.recordings && response.data.recordings.length > 0) {
      const recording = response.data.recordings[0];
      return await this.getArtworkFromRecording(recording);
    }

    return null;
  }

  /**
   * Get artwork from recording
   */
  async getArtworkFromRecording(recording) {
    // Get release from recording
    if (recording.releases && recording.releases.length > 0) {
      const release = recording.releases[0];
      return await this.getArtworkFromRelease(release);
    }

    // If recording has release ID, fetch full release data
    if (recording['release-list'] && recording['release-list'].length > 0) {
      const releaseId = recording['release-list'][0].id;
      return await this.getArtworkByReleaseId(releaseId);
    }

    return null;
  }

  /**
   * Get artwork from release
   */
  async getArtworkFromRelease(release) {
    if (release.id) {
      return await this.getArtworkByReleaseId(release.id);
    }
    return null;
  }

  /**
   * Get artwork from Cover Art Archive by release ID
   */
  async getArtworkByReleaseId(releaseId) {
    await this.rateLimit();

    logger.debug(`Fetching cover art for release: ${releaseId}`);

    try {
      const response = await retry(async () => {
        return await axios.get(`${this.coverArtUrl}/release/${releaseId}`, {
          timeout: this.timeout,
          headers: {
            'User-Agent': this.userAgent
          }
        });
      }, {
        retries: 2,
        shouldRetry: (error) => {
          // Cover Art Archive returns 404 if no artwork
          if (error.response && error.response.status === 404) {
            return false;
          }
          return isRetryableError(error);
        }
      });

      const data = response.data;

      // Find front cover
      const frontCover = data.images.find(img => img.front === true) || data.images[0];

      if (frontCover) {
        return {
          url: frontCover.thumbnails?.large || frontCover.image,
          hires: frontCover.image,
          uncompressed: frontCover.image, // Cover Art Archive provides original
          title: releaseId,
          width: 500,
          height: 500,
          provider: 'MusicBrainz',
          releaseId: releaseId,
          rawData: frontCover
        };
      }
    } catch (error) {
      if (error.response && error.response.status === 404) {
        logger.debug(`No cover art found for release: ${releaseId}`);
      } else {
        logger.debug('Cover Art Archive fetch failed', { error: error.message });
      }
    }

    return null;
  }

  /**
   * Get the best quality artwork URL from result
   */
  getBestQualityUrl(artworkData) {
    return artworkData.uncompressed || artworkData.hires || artworkData.url;
  }
}

export default MusicBrainzProvider;
