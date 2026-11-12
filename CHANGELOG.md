## v0.4.1 (2026-11-12)

### Fix

- **cli**: ask clean confirmations with rich prompts (#14)

## v0.4.0 (2026-11-12)

### Feat

- **cli**: show what each cache holds with -v (#12)
- **plugins**: add a Ruby plugin for old gem versions

### Refactor

- **plugins**: describe plugins with plainly named fields (#13)

## v0.3.0 (2026-11-11)

### Feat

- **plugins**: add a projects plugin and a config file (#11)
- **plugins**: remove the Ansible plugin
- **plugins**: skip a plugin while its tool is running (#10)
- **plugins**: add a Poetry cache and virtualenvs plugin (#9)

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
