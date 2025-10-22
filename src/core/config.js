import { readFileSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const rootDir = join(__dirname, '../..');

/**
 * Configuration manager for the music recovery tool
 */
class Config {
  constructor() {
    this.config = this.loadConfig();
  }

  loadConfig() {
    const configPath = join(rootDir, 'config/default.json');

    // Default configuration
    const defaults = {
      artwork: {
        providers: {
          itunes: {
            enabled: true,
            baseUrl: 'https://itunes.apple.com',
            rateLimit: 20, // requests per second
            timeout: 10000,
            resolutions: {
              standard: '600x600',
              hires: '100000x100000-999',
              uncompressed: true // for albums
            }
          },
          spotify: {
            enabled: false,
            clientId: process.env.SPOTIFY_CLIENT_ID || '',
            clientSecret: process.env.SPOTIFY_CLIENT_SECRET || '',
            rateLimit: 180, // requests per minute
            timeout: 10000
          },
          musicbrainz: {
            enabled: true,
            baseUrl: 'https://musicbrainz.org/ws/2',
            coverArtUrl: 'https://coverartarchive.org',
            rateLimit: 1, // requests per second
            timeout: 10000,
            userAgent: 'MusicRecoveryTool/1.0.0 (contact@example.com)'
          }
        },
        outputDir: join(rootDir, 'data/output/artwork'),
        fileNameFormat: '{artist} - {title}', // or {isrc}, {upc}
        preferredFormat: 'jpg',
        maxConcurrent: 5,
        retryAttempts: 3,
        retryDelay: 2000 // ms
      },
      audio: {
        enabled: false,
        outputDir: join(rootDir, 'data/output/audio'),
        format: 'wav'
      },
      lyrics: {
        enabled: false,
        outputDir: join(rootDir, 'data/output/lyrics'),
        format: 'lrc'
      },
      csv: {
        inputDir: join(rootDir, 'data/input'),
        batchSize: 50,
        delimiter: ',',
        encoding: 'utf8',
        headers: ['title', 'artist', 'upc', 'isrc', 'entity_type']
      },
      logging: {
        level: 'info', // error, warn, info, debug
        outputDir: join(rootDir, 'data/output/reports'),
        console: true,
        file: true
      },
      paths: {
        root: rootDir,
        data: join(rootDir, 'data'),
        input: join(rootDir, 'data/input'),
        output: join(rootDir, 'data/output')
      }
    };

    // Load user config if exists
    if (existsSync(configPath)) {
      try {
        const userConfig = JSON.parse(readFileSync(configPath, 'utf8'));
        return this.mergeDeep(defaults, userConfig);
      } catch (error) {
        console.warn(`Warning: Could not load config from ${configPath}, using defaults`);
        return defaults;
      }
    }

    return defaults;
  }

  /**
   * Deep merge two objects
   */
  mergeDeep(target, source) {
    const output = { ...target };
    if (this.isObject(target) && this.isObject(source)) {
      Object.keys(source).forEach(key => {
        if (this.isObject(source[key])) {
          if (!(key in target)) {
            Object.assign(output, { [key]: source[key] });
          } else {
            output[key] = this.mergeDeep(target[key], source[key]);
          }
        } else {
          Object.assign(output, { [key]: source[key] });
        }
      });
    }
    return output;
  }

  isObject(item) {
    return item && typeof item === 'object' && !Array.isArray(item);
  }

  get(path) {
    const keys = path.split('.');
    let value = this.config;

    for (const key of keys) {
      if (value && typeof value === 'object' && key in value) {
        value = value[key];
      } else {
        return undefined;
      }
    }

    return value;
  }

  set(path, newValue) {
    const keys = path.split('.');
    let current = this.config;

    for (let i = 0; i < keys.length - 1; i++) {
      const key = keys[i];
      if (!(key in current)) {
        current[key] = {};
      }
      current = current[key];
    }

    current[keys[keys.length - 1]] = newValue;
  }

  getAll() {
    return this.config;
  }
}

// Singleton instance
const config = new Config();

export default config;
