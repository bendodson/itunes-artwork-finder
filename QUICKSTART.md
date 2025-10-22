# Quick Start Guide - Music Recovery Tool

## 🚀 Get Started in 3 Minutes

### Step 1: Install Dependencies

```bash
npm install
```

### Step 2: Prepare Your CSV

Create a file in `data/input/my-tracks.csv`:

```csv
title,artist,upc,isrc,entity_type
Thriller,Michael Jackson,00074643811224,,album
Abbey Road,The Beatles,00094638241720,,album
```

**Tips:**
- Always include `title` and `artist` (required)
- Add `isrc` for best accuracy (recommended)
- Add `upc` for albums (recommended)
- `entity_type` defaults to "album" if omitted

### Step 3: Run Artwork Extraction

```bash
npm run artwork data/input/my-tracks.csv
```

Or use the main CLI:

```bash
npm start -- artwork data/input/my-tracks.csv
```

### Step 4: Check Results

**Downloaded Artwork:**
```bash
ls data/output/artwork/
```

**Processing Log:**
```bash
cat data/output/reports/$(date +%Y-%m-%d).log
```

**CSV Report:**
```bash
cat data/output/reports/artwork-report-*.csv
```

---

## 📊 Understanding the Output

### Console Output

```
[INFO] Starting artwork module
[INFO] Processing CSV file: data/input/my-tracks.csv
[INFO] Searching artwork | title: Thriller, artist: Michael Jackson
[INFO] Artwork found via iTunes | Thriller - Michael Jackson
[SUCCESS] Artwork batch complete: 10 succeeded, 0 failed
```

### Downloaded Files

Location: `data/output/artwork/`

Filename format: `{artist} - {title}.jpg`

Example: `Michael Jackson - Thriller.jpg`

### CSV Report

Location: `data/output/reports/artwork-report-*.csv`

Contains:
- `title`, `artist`, `isrc`, `upc` - Your input data
- `success` - true/false
- `provider` - Which API was used (iTunes, Spotify, MusicBrainz)
- `filePath` - Where the file was saved
- `error` - Error message if failed

---

## 🎛️ Configuration (Optional)

Edit `config/default.json` to customize:

### Enable Spotify (Higher Quality)

1. Get free API credentials from https://developer.spotify.com/dashboard
2. Update config:

```json
{
  "artwork": {
    "providers": {
      "spotify": {
        "enabled": true,
        "clientId": "YOUR_CLIENT_ID",
        "clientSecret": "YOUR_CLIENT_SECRET"
      }
    }
  }
}
```

### Change Filename Format

```json
{
  "artwork": {
    "fileNameFormat": "{artist} - {album}"
  }
}
```

Options: `{title}`, `{artist}`, `{album}`, `{isrc}`, `{upc}`

### Adjust Performance

```json
{
  "artwork": {
    "maxConcurrent": 10
  },
  "csv": {
    "batchSize": 100
  }
}
```

---

## 🔧 Common Commands

```bash
# List available CSV files
npm start -- list

# Run artwork extraction
npm run artwork data/input/my-tracks.csv

# See help
npm start -- --help

# Check version
npm start -- --version
```

---

## ❓ Troubleshooting

### No Artwork Found?

**Try:**
1. Verify UPC/ISRC codes are correct
2. Check if the track exists on iTunes manually
3. Enable MusicBrainz in config (slower but more reliable)
4. Check the log file for API errors

### Spotify Not Working?

**Check:**
1. Client ID and Secret are correct
2. No extra spaces in credentials
3. Set `"enabled": true` in config

### Files Already Exist?

The tool skips existing files by default. Delete files in `data/output/artwork/` to re-download.

---

## 📚 Full Documentation

See [README-AUTOMATION.md](./README-AUTOMATION.md) for:
- Complete architecture details
- All configuration options
- API provider documentation
- Extending the system
- Best practices

---

## 🎯 Next Steps

1. **Process your full catalog** - Once tested, run on your complete CSV
2. **Enable Spotify** - For higher quality artwork
3. **Customize filenames** - Match your preferred organization
4. **Review failures** - Check CSV reports for failed tracks

---

## 💡 Pro Tips

**For Best Results:**
- Always include ISRC codes (most accurate identifier)
- Include UPC for albums
- Use correct spelling for artist/title
- Start with a small batch (10-20 tracks) to test

**Performance:**
- Increase `maxConcurrent` for faster processing
- Increase `batchSize` for large catalogs (1000+ tracks)
- Disable MusicBrainz if speed is critical (it's limited to 1 req/sec)

**Organization:**
- Use filename template `{artist} - {album}` for album artwork
- Use `{isrc}` for unique filenames
- Check `data/output/reports/*.log` for detailed progress

---

**Ready to recover your music metadata! 🎵**
