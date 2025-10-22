import axios from 'axios';
import logger from '../../../core/logger.js';
import { retry, isRetryableError } from '../../../utils/retry.js';

/**
 * iTunes Search API Provider
 * Ported from api.php with enhanced functionality
 */
class iTunesProvider {
  constructor(config = {}) {
    this.name = 'iTunes';
    this.baseUrl = config.baseUrl || 'https://itunes.apple.com';
    this.timeout = config.timeout || 10000;
    this.enabled = config.enabled !== false;
    this.resolutions = config.resolutions || {
      standard: '600x600',
      hires: '100000x100000-999',
      uncompressed: true
    };

    // Support capabilities
    this.supportsISRC = false; // iTunes API doesn't support ISRC directly
    this.supportsUPC = true;
  }

  /**
   * Search for artwork using various parameters
   * Ported from api.php lines 4-28
   */
  async search(params) {
    const { query, entity, country, upc, isrc } = params;

    try {
      // If UPC is provided, use lookup endpoint (preferred)
      if (upc) {
        return await this.searchByUPC(upc, country);
      }

      // Fallback to text search
      if (query || (params.title && params.artist)) {
        const searchQuery = query || `${params.artist} ${params.title}`;
        return await this.searchByText(searchQuery, entity, country);
      }

      throw new Error('Insufficient search parameters. Need UPC or title+artist');
    } catch (error) {
      logger.error(`iTunes search failed`, {
        params,
        error: error.message
      });
      return null;
    }
  }

  /**
   * Search by UPC/EAN barcode
   * Corresponds to api.php lines 22-23
   */
  async searchByUPC(upc, country = 'us') {
    const url = `${this.baseUrl}/lookup`;

    logger.debug(`iTunes UPC lookup: ${upc}`);

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          upc: upc,
          country: country,
          limit: 25
        },
        timeout: this.timeout
      });
    }, {
      retries: 3,
      shouldRetry: isRetryableError
    });

    return this.processResults(response.data, 'album');
  }

  /**
   * Search by text query
   * Corresponds to api.php lines 17-20
   */
  async searchByText(searchQuery, entity = 'album', country = 'us') {
    let apiEntity = entity;
    let searchParams = {
      term: searchQuery,
      country: country,
      entity: apiEntity,
      limit: 25
    };

    // Handle special entity types
    const shortFilm = entity === 'shortFilm';
    if (shortFilm) {
      apiEntity = 'movie';
      searchParams.entity = 'movie';
      searchParams.attribute = 'shortFilmTerm';
    }

    const url = `${this.baseUrl}/search`;

    logger.debug(`iTunes text search: ${searchQuery} (${entity})`);

    const response = await retry(async () => {
      return await axios.get(url, {
        params: searchParams,
        timeout: this.timeout
      });
    }, {
      retries: 3,
      shouldRetry: isRetryableError
    });

    return this.processResults(response.data, entity);
  }

  /**
   * Process iTunes API results and extract artwork URLs
   * Ported from api.php lines 31-125
   */
  processResults(data, entity) {
    if (!data || !data.results || data.results.length === 0) {
      logger.debug('No results from iTunes API');
      return null;
    }

    const results = [];

    for (const result of data.results) {
      // Filter by entity type for ID lookups (api.php lines 40-46)
      if (entity === 'id' && result.kind !== 'feature-movie' && result.wrapperType !== 'collection') {
        continue;
      }

      if (entity === 'idAlbum' && result.collectionType !== 'Album') {
        continue;
      }

      const artworkData = this.extractArtwork(result, entity);
      if (artworkData) {
        results.push(artworkData);
      }
    }

    return results.length > 0 ? results[0] : null; // Return first result
  }

  /**
   * Extract artwork URLs from iTunes result
   * Ported from api.php lines 48-120
   */
  extractArtwork(result, entity) {
    if (!result.artworkUrl100) {
      return null;
    }

    let width = 600;
    let height = 600;

    // Standard resolution (api.php line 52)
    const standardUrl = result.artworkUrl100.replace('100x100', this.resolutions.standard);

    // High resolution (api.php lines 54-56)
    const hiresUrl = result.artworkUrl100.replace('100x100bb', this.resolutions.hires);
    const hiresUrlParts = new URL(hiresUrl);
    const finalHiresUrl = `https://is5-ssl.mzstatic.com${hiresUrlParts.pathname}`;

    // Get title based on entity type (api.php line 59)
    let title = (entity === 'movie') ? result.trackName : result.collectionName;

    const data = {
      url: standardUrl,
      hires: finalHiresUrl,
      title: title,
      width: width,
      height: height,
      provider: 'iTunes',
      rawData: result
    };

    // Uncompressed URL for albums (api.php lines 61-68)
    if ((entity === 'album' || entity === 'idAlbum') && this.resolutions.uncompressed) {
      const uncompressed = this.getUncompressedUrl(finalHiresUrl);
      if (uncompressed) {
        data.uncompressed = uncompressed;
      }
    }

    // Entity-specific processing (api.php lines 70-114)
    switch (entity) {
      case 'musicVideo':
        data.title = `${result.trackName} (by ${result.artistName})`;
        data.url = finalHiresUrl;
        data.width = 640;
        data.height = 464;
        break;

      case 'tvSeason':
        data.title = result.collectionName;
        break;

      case 'movie':
      case 'id':
      case 'shortFilm':
        data.title = result.trackName || result.collectionName;
        data.width = 400;
        break;

      case 'ebook':
        data.title = `${result.trackName} (by ${result.artistName})`;
        data.width = 400;
        break;

      case 'album':
      case 'idAlbum':
        data.title = `${result.collectionName} (by ${result.artistName})`;
        data.album = result.collectionName;
        data.artist = result.artistName;
        break;

      case 'audiobook':
      case 'podcast':
        data.title = `${result.collectionName} (by ${result.artistName})`;
        break;

      case 'software':
      case 'iPadSoftware':
      case 'macSoftware':
        if (result.artworkUrl512) {
          data.url = result.artworkUrl512.replace('512x512bb', '1024x1024bb');
        }
        data.title = result.trackName;
        data.appstore = result.trackViewUrl;
        data.width = 512;
        data.height = 512;
        break;
    }

    return data.title ? data : null;
  }

  /**
   * Extract uncompressed URL for albums
   * Ported from api.php lines 62-67
   */
  getUncompressedUrl(hiresUrl) {
    try {
      const parts = hiresUrl.split('/image/thumb/');
      if (parts.length === 2) {
        const pathParts = parts[1].split('/');
        pathParts.pop(); // Remove last element (size specification)
        return `https://a5.mzstatic.com/us/r1000/0/${pathParts.join('/')}`;
      }
    } catch (error) {
      logger.debug('Could not extract uncompressed URL', { error: error.message });
    }
    return null;
  }

  /**
   * Get the best quality artwork URL from result
   */
  getBestQualityUrl(artworkData) {
    if (artworkData.uncompressed) {
      return artworkData.uncompressed;
    }
    if (artworkData.hires) {
      return artworkData.hires;
    }
    return artworkData.url;
  }
}

export default iTunesProvider;
