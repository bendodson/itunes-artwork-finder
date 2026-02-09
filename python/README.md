# Python Album Cache Tools

This directory contains Python CLI tools for building a local album-art cache
from an Apple Music / iTunes library.

Phases:
- Phase 1: Extract albums + representative tracks from Library.xml
- Phase 2: Resolve collectionId + artwork URLs using a single iTunes Search call
- Phase 3: Generate widescreen artwork for Samsung Frame TV

All scripts are designed to be:
- resumable
- rate-limit aware
- polite to Apple APIs
