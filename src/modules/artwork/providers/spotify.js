import axios from 'axios';
import logger from '../../../core/logger.js';
import { retry, isRetryableError } from '../../../utils/retry.js';

/**
 * Spotify Web API Provider
 * Supports ISRC lookups (most accurate)
 * Requires client credentials
 */
class SpotifyProvider {
  constructor(config = {}) {
    this.name = 'Spotify';
    this.baseUrl = 'https://api.spotify.com/v1';
    this.authUrl = 'https://accounts.spotify.com/api/token';
    this.clientId = config.clientId || process.env.SPOTIFY_CLIENT_ID;
    this.clientSecret = config.clientSecret || process.env.SPOTIFY_CLIENT_SECRET;
    this.timeout = config.timeout || 10000;
    this.enabled = config.enabled !== false && this.clientId && this.clientSecret;

    // Support capabilities
    this.supportsISRC = true;
    this.supportsUPC = false;

    // Access token cache
    this.accessToken = null;
    this.tokenExpiry = null;
  }

  /**
   * Get or refresh access token
   */
  async getAccessToken() {
    // Return cached token if still valid
    if (this.accessToken && this.tokenExpiry && Date.now() < this.tokenExpiry) {
      return this.accessToken;
    }

    logger.debug('Requesting Spotify access token');

    const auth = Buffer.from(`${this.clientId}:${this.clientSecret}`).toString('base64');

    const response = await axios.post(
      this.authUrl,
      'grant_type=client_credentials',
      {
        headers: {
          'Authorization': `Basic ${auth}`,
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        timeout: this.timeout
      }
    );

    this.accessToken = response.data.access_token;
    this.tokenExpiry = Date.now() + (response.data.expires_in * 1000) - 60000; // Refresh 1 min early

    logger.debug('Spotify access token obtained');

    return this.accessToken;
  }

  /**
   * Search for artwork using various parameters
   */
  async search(params) {
    if (!this.enabled) {
      logger.debug('Spotify provider not enabled (missing credentials)');
      return null;
    }

    const { isrc, title, artist } = params;

    try {
      // Try ISRC first (most accurate for Spotify)
      if (isrc) {
        const result = await this.searchByISRC(isrc);
        if (result) return result;
      }

      // Fallback to text search
      if (title && artist) {
        return await this.searchByText(title, artist);
      }

      return null;
    } catch (error) {
      logger.error('Spotify search failed', {
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
    const token = await this.getAccessToken();

    logger.debug(`Spotify ISRC lookup: ${isrc}`);

    const url = `${this.baseUrl}/search`;

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          q: `isrc:${isrc}`,
          type: 'track',
          limit: 1
        },
        headers: {
          'Authorization': `Bearer ${token}`
        },
        timeout: this.timeout
      });
    }, {
      retries: 3,
      shouldRetry: isRetryableError
    });

    if (response.data.tracks && response.data.tracks.items.length > 0) {
      const track = response.data.tracks.items[0];
      return this.extractArtwork(track);
    }

    return null;
  }

  /**
   * Search by artist and title
   */
  async searchByText(title, artist) {
    const token = await this.getAccessToken();

    logger.debug(`Spotify text search: ${artist} - ${title}`);

    const url = `${this.baseUrl}/search`;

    const response = await retry(async () => {
      return await axios.get(url, {
        params: {
          q: `track:${title} artist:${artist}`,
          type: 'track',
          limit: 1
        },
        headers: {
          'Authorization': `Bearer ${token}`
        },
        timeout: this.timeout
      });
    }, {
      retries: 3,
      shouldRetry: isRetryableError
    });

    if (response.data.tracks && response.data.tracks.items.length > 0) {
      const track = response.data.tracks.items[0];
      return this.extractArtwork(track);
    }

    return null;
  }

  /**
   * Extract artwork from Spotify track
   */
  extractArtwork(track) {
    if (!track.album || !track.album.images || track.album.images.length === 0) {
      return null;
    }

    // Spotify images are sorted by size (largest first)
    const images = track.album.images;
    const largestImage = images[0];
    const mediumImage = images[1] || images[0];

    return {
      url: mediumImage.url,
      hires: largestImage.url,
      uncompressed: largestImage.url,
      title: `${track.album.name} (by ${track.artists[0].name})`,
      album: track.album.name,
      artist: track.artists[0].name,
      width: largestImage.width || 640,
      height: largestImage.height || 640,
      provider: 'Spotify',
      spotifyId: track.id,
      albumId: track.album.id,
      rawData: track
    };
  }

  /**
   * Get the best quality artwork URL from result
   */
  getBestQualityUrl(artworkData) {
    return artworkData.hires || artworkData.url;
  }
}

export default SpotifyProvider;
