## v0.2.0 (2026-11-10)

### Feat

- **plugins**: add JavaScript and Apple developer tool plugins
- **cli**: sort the cache table by name
- **cli**: drop the skipped-plugin list below the cache table
- **cli**: use a minimal style for the cache tables
- **cli**: list each plugin on one line in help
- **plugins**: add ten cache plugins (#7)
- **cli**: show one row per tool and a before and after clean table (#5)
- **plugins**: add eight cache plugins and brew autoremove (#4)
- add the purger cli for finding and cleaning tool caches (#3)

### Fix

- **plugins**: count brew formula folders in the footprint (#8)
- **plugins**: find old Playwright builds when measured, not at import
- **plugins**: skip docker and podman when the engine is remote
- **plugins**: correct cache detection and the brew size total
- **clean**: clarify failure messages and correct partial-purge marks (#6)
