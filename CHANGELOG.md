# Changelog

## 2.0.1
- Readable error page instead of a generic 500
- Clear message when the config folder isn't writable (wrong owner on the /config mount)
- Portainer guide and a ready-to-paste stack file
- Notes on the two setup traps: a bind mount pointing at a path that doesn't exist,
  and a config folder owned by root

## 2.0
- Item page for editing ABS metadata (through the ABS API)
- Write metadata, cover and chapters into audio files using ABS's embed tool
- View and bulk-edit embedded tags directly (mp3, m4b, flac, ogg) with mutagen
- View and edit metadata.json, with automatic backups
- Rename and move folders, rename files, move files or whole items to a trash folder
- Path mapping between ABS paths and local paths
- Change log, per-run form token, path containment checks
- Editing is off by default

## 1.0
- Duplicate scan across Audiobookshelf book libraries
- Match on ASIN/ISBN, normalised title and author, subtitle differences,
  fuzzy titles and running time
- Side-by-side comparison with differing fields highlighted
- Filters, search, "Not duplicates" dismissals and CSV export
