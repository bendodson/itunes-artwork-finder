# Music Recovery Tool - Automated Batch Processing

> **Automated, modular system for recovering music metadata from CSV files**
>
> Built on the iTunes Artwork Finder codebase with enterprise-grade batch processing capabilities.

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Usage](#usage)
- [Configuration](#configuration)
- [CSV Format](#csv-format)
- [API Providers](#api-providers)
- [Troubleshooting](#troubleshooting)

---

## Overview

This tool automates the extraction of music metadata (artwork, audio, lyrics) from CSV files containing track information. It's designed for copyright holders recovering their own content from distribution services.

### What It Does

1. **Artwork Module** ✅ Fully Implemented
   - Downloads high-resolution album/track artwork
   - Supports multiple APIs with automatic fallback
   - Handles ISRC, UPC, and text-based search
   - Batch processing with progress tracking

2. **Audio Module** 🚧 Placeholder (Future)
   - Framework ready for distributor API integration
   - Support for DistroKid, TuneCore, CD Baby, etc.

3. **Lyrics Module** 🚧 Placeholder (Future)
   - Framework ready for lyrics API integration
   - Support for Musixmatch, Genius, etc.

---

## Features

### Core Capabilities

- ✅ **Multi-Provider Fallback Chain**
  - Spotify API (ISRC lookup - highest quality)
  - MusicBrainz + Cover Art Archive (ISRC/UPC - most reliable)
  - iTunes Search API (text search - broadest coverage)

- ✅ **Intelligent Search Strategy**
  - ISRC → UPC → Title+Artist fallback hierarchy
  - Automatic provider selection based on available data
  - Retry logic with exponential backoff

- ✅ **Enterprise-Grade Processing**
  - Streaming CSV parser (handles millions of rows)
  - Configurable batch sizes and concurrency
  - Rate limiting per API (respects provider limits)
  - Progress tracking and detailed logging

- ✅ **Robust Error Handling**
  - Individual track failures don't stop batch
  - Detailed error logging with context
  - CSV reports for success/failure tracking
  - Resume capability (skips existing files)

### Technical Highlights

- **Modular Architecture**: Plugin-based design for easy extension
- **ESM Modules**: Modern JavaScript (Node.js ≥18)
- **Type Safety Ready**: Structured for TypeScript migration
- **Clean Code**: Based on existing proven codebase (api.php → Node.js)

---

## Quick Start

### 1. Installation

```bash
# Install dependencies
npm install

# Verify installation
npm start -- --version
```

### 2. Prepare Your CSV

Create a CSV file in `data/input/` with your track information:

```csv
title,artist,upc,isrc,entity_type
My Song,My Artist,00012345678905,USRC12345678,album
Another Track,My Artist,,GBUM71234567,album
```

### 3. Run Artwork Extraction

```bash
# Process a single CSV file
npm run artwork data/input/my-tracks.csv

# Or use the main command
npm start -- artwork data/input/my-tracks.csv
```

### 4. Check Results

- **Downloaded Artwork**: `data/output/artwork/`
- **Processing Log**: `data/output/reports/YYYY-MM-DD.log`
- **CSV Report**: `data/output/reports/artwork-report-*.csv`

---

## Architecture

### Directory Structure

```
music-recovery-tool/
├── src/
│   ├── core/                    # Core utilities
│   │   ├── config.js           # Configuration management
│   │   ├── logger.js           # Logging with file/console output
│   │   ├── downloader.js       # File download utility
│   │   └── csv-processor.js    # Streaming CSV parser
│   ├── modules/
│   │   ├── artwork/            # Artwork extraction module
│   │   │   ├── index.js        # Main orchestrator
│   │   │   └── providers/      # API providers
│   │   │       ├── itunes.js   # iTunes Search API (from api.php)
│   │   │       ├── spotify.js  # Spotify Web API
│   │   │       └── musicbrainz.js # MusicBrainz + Cover Art Archive
│   │   ├── audio/              # Audio module (placeholder)
│   │   └── lyrics/             # Lyrics module (placeholder)
│   ├── utils/
│   │   ├── rate-limiter.js     # API rate limiting
│   │   └── retry.js            # Retry logic with backoff
│   └── index.js                # CLI entry point
├── data/
│   ├── input/                  # Place your CSV files here
│   └── output/
│       ├── artwork/            # Downloaded artwork
│       ├── audio/              # Audio files (future)
│       ├── lyrics/             # Lyrics files (future)
│       └── reports/            # Logs and CSV reports
├── config/
│   └── default.json            # Configuration file
└── package.json
```

### Data Flow

```
CSV File
    ↓
[CSV Parser] → Batches of 50 tracks
    ↓
[Artwork Module]
    ↓
[Provider Chain: Spotify → MusicBrainz → iTunes]
    ↓
[Rate Limiter] → API Request
    ↓
[Downloader] → Save to disk
    ↓
[Logger] → Reports & Logs
```

---

## Usage

### CLI Commands

```bash
# List available CSV files
npm start -- list

# Process artwork only
npm start -- artwork data/input/tracks.csv

# Process all enabled modules
npm start -- all data/input/tracks.csv

# Audio module (not yet implemented)
npm start -- audio data/input/tracks.csv

# Lyrics module (not yet implemented)
npm start -- lyrics data/input/tracks.csv
```

### Programmatic Usage

```javascript
import MusicRecoveryTool from './src/index.js';

const tool = new MusicRecoveryTool();

// Run artwork module
const results = await tool.runModule('artwork', 'data/input/tracks.csv');

console.log(`Processed: ${results.total}`);
console.log(`Succeeded: ${results.succeeded}`);
console.log(`Failed: ${results.failed}`);
```

---

## Configuration

Edit `config/default.json` to customize behavior:

### Artwork Configuration

```json
{
  "artwork": {
    "providers": {
      "itunes": {
        "enabled": true,
        "rateLimit": 20,
        "timeout": 10000
      },
      "spotify": {
        "enabled": false,
        "clientId": "YOUR_CLIENT_ID",
        "clientSecret": "YOUR_CLIENT_SECRET",
        "rateLimit": 180
      },
      "musicbrainz": {
        "enabled": true,
        "rateLimit": 1,
        "userAgent": "MusicRecoveryTool/1.0.0 (your-email@example.com)"
      }
    },
    "fileNameFormat": "{artist} - {title}",
    "maxConcurrent": 5,
    "retryAttempts": 3
  }
}
```

### Filename Templates

Available placeholders:
- `{title}` - Track title
- `{artist}` - Artist name
- `{album}` - Album name
- `{isrc}` - ISRC code
- `{upc}` - UPC barcode

Example: `"{artist} - {album}"` → `"Taylor Swift - 1989.jpg"`

### Logging Levels

- `error` - Only errors
- `warn` - Warnings and errors
- `info` - General information (recommended)
- `debug` - Detailed debugging output

---

## CSV Format

### Required Columns

| Column | Required | Description | Example |
|--------|----------|-------------|---------|
| `title` | ✅ Yes | Track or album title | "Bohemian Rhapsody" |
| `artist` | ✅ Yes | Artist name | "Queen" |
| `upc` | ⚠️ Recommended | UPC/EAN barcode | "00012345678905" |
| `isrc` | ⚠️ Recommended | ISRC code | "GBUM71234567" |
| `entity_type` | No | Media type (default: album) | "album", "movie", "ebook" |

### Sample CSV

```csv
title,artist,upc,isrc,entity_type
A Night at the Opera,Queen,00012345678905,GBUM71234567,album
Thriller,Michael Jackson,00077778889999,USCJ12345678,album
Back in Black,AC/DC,,AUMC12345678,album
```

### Tips for Best Results

1. **Always include ISRC if available** - Most accurate identifier
2. **Include UPC for albums** - Good fallback for MusicBrainz
3. **Ensure correct spelling** - Text search is fuzzy but not magic
4. **Use entity_type for non-music** - "movie", "ebook", "audiobook", etc.

---

## API Providers

### 1. iTunes Search API

**Status**: ✅ Fully Implemented (ported from `api.php`)

- **Supports**: UPC, text search (title + artist)
- **Does NOT support**: ISRC
- **Rate Limit**: ~20 requests/second
- **Quality**: Medium-High (up to 1200x1200 for albums)
- **Auth**: Not required
- **Cost**: Free

**Best For**: Broad coverage, no API keys needed

### 2. Spotify Web API

**Status**: ✅ Implemented (optional, requires credentials)

- **Supports**: ISRC, text search
- **Does NOT support**: UPC
- **Rate Limit**: 180 requests/minute (with backoff)
- **Quality**: High (640x640 typical, up to 1000x1000)
- **Auth**: OAuth 2.0 Client Credentials
- **Cost**: Free

**Setup**:
1. Create app at https://developer.spotify.com/dashboard
2. Get Client ID and Client Secret
3. Add to `config/default.json` or environment variables:
   ```bash
   export SPOTIFY_CLIENT_ID="your_client_id"
   export SPOTIFY_CLIENT_SECRET="your_client_secret"
   ```
4. Enable in config: `"spotify": { "enabled": true }`

**Best For**: ISRC lookups, highest quality artwork

### 3. MusicBrainz + Cover Art Archive

**Status**: ✅ Fully Implemented

- **Supports**: ISRC, UPC, text search
- **Rate Limit**: 1 request/second (strictly enforced)
- **Quality**: High (original resolution, up to 1200px thumbnails)
- **Auth**: Not required
- **Cost**: Free

**Important**: Update the `userAgent` in config with your contact email

**Best For**: Reliability, open-source friendly, supports both ISRC and UPC

### Provider Priority Order

The system tries providers in this order:

1. **Spotify** (if enabled) - Best for ISRC, highest quality
2. **MusicBrainz** - Most reliable, supports both ISRC/UPC
3. **iTunes** - Broadest coverage, fallback for text search

---

## Extending the System

### Adding a New Artwork Provider

1. Create provider in `src/modules/artwork/providers/`:

```javascript
// src/modules/artwork/providers/deezer.js
import axios from 'axios';

class DeezerProvider {
  constructor(config = {}) {
    this.name = 'Deezer';
    this.supportsISRC = true;
    this.supportsUPC = false;
  }

  async search(params) {
    // Implementation
  }

  getBestQualityUrl(artworkData) {
    return artworkData.hires || artworkData.url;
  }
}

export default DeezerProvider;
```

2. Register in `src/modules/artwork/index.js`:

```javascript
import DeezerProvider from './providers/deezer.js';

// In initializeProviders()
if (providerConfigs.deezer?.enabled) {
  providers.push(new DeezerProvider(providerConfigs.deezer));
}
```

3. Add config in `config/default.json`

### Implementing Audio/Lyrics Modules

The framework is ready. Simply implement the `processCSV(csvPath)` method in:
- `src/modules/audio/index.js`
- `src/modules/lyrics/index.js`

Follow the pattern used in `src/modules/artwork/index.js`

---

## Troubleshooting

### No Artwork Found

**Check**:
1. Are ISRC/UPC codes correct? Verify on iTunes/Spotify manually
2. Is the track actually available? Check iTunes Store directly
3. Try different providers - enable Spotify or MusicBrainz
4. Check logs in `data/output/reports/` for API errors

### Rate Limit Errors

**Solution**:
- Reduce `maxConcurrent` in config
- Increase `retryDelay`
- For MusicBrainz: It strictly enforces 1 req/sec (already handled)

### Spotify Authentication Errors

**Check**:
1. Client ID and Secret are correct
2. No extra spaces in credentials
3. App is not in development mode restrictions

### Large CSV Files Taking Too Long

**Optimize**:
1. Increase `batchSize` (default: 50)
2. Increase `maxConcurrent` (default: 5)
3. Disable slower providers (MusicBrainz is limited to 1 req/sec)
4. Run during off-peak hours

### Missing Dependencies

```bash
# Clean install
rm -rf node_modules package-lock.json
npm install
```

---

## Best Practices

### For Copyright Holders Recovering Content

1. **Organize Your Data**
   - Export track lists from your distributor
   - Include ISRC codes (most accurate)
   - Keep UPC codes for albums

2. **Start Small**
   - Test with 10-20 tracks first
   - Verify output quality and filenames
   - Adjust configuration as needed

3. **Monitor Progress**
   - Watch console output for errors
   - Check `data/output/reports/*.log` files
   - Review CSV reports for failed tracks

4. **Handle Failures**
   - Export failed tracks from report CSV
   - Verify data accuracy
   - Retry with corrected information

### Performance Tuning

For **1000+ tracks**:
```json
{
  "artwork": {
    "maxConcurrent": 10,
    "providers": {
      "spotify": { "enabled": true },
      "musicbrainz": { "enabled": false }
    }
  },
  "csv": {
    "batchSize": 100
  }
}
```

For **Accuracy over Speed**:
```json
{
  "artwork": {
    "maxConcurrent": 3,
    "providers": {
      "musicbrainz": { "enabled": true }
    }
  }
}
```

---

## Credits

- **Original iTunes Artwork Finder**: `api.php` and `itunes.js` logic ported to Node.js
- **MusicBrainz**: Open-source music metadata database
- **Cover Art Archive**: Community-maintained cover art repository
- **Spotify**: Web API for music metadata

---

## License

MIT License (same as original iTunes Artwork Finder)

---

## Support

For issues or questions:
1. Check logs in `data/output/reports/`
2. Review this documentation
3. Check API provider status pages
4. Verify your CSV data accuracy

---

## Roadmap

- [x] Artwork Module with multi-provider support
- [x] CSV batch processing
- [x] Progress tracking and reporting
- [ ] Audio Module (distributor API integration)
- [ ] Lyrics Module (Musixmatch/Genius integration)
- [ ] TypeScript migration
- [ ] Web UI for monitoring
- [ ] Resume from checkpoint (partial CSV processing)
- [ ] Duplicate detection
- [ ] Image quality validation

---

**Happy recovering! 🎵**
