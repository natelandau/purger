# purger

purger finds the caches that developer tools leave on your disk, shows how much space each cache uses, and removes the caches that you approve. It uses the cleanup command of each tool when the tool has one. It deletes a cache directory by hand only when no command exists, and only inside your home directory.

Without `--dangerous` and your explicit confirmation, purger never deletes your files, your projects, or data that cannot be rebuilt.

## What purger cleans

purger runs on macOS and Linux. Each supported tool is a plugin, and each plugin has one or more cleanup actions.

- `ansible` (macOS, Linux): temp folders left by interrupted runs and the Galaxy cache, in `~/.ansible` and `$ANSIBLE_HOME`. Skipped while an ansible command runs.
- `apt` (Linux): downloaded package files. Both actions run with sudo.
- `brew` (macOS, Linux): old formula versions, the download cache, and dependencies that nothing needs anymore.
- `conda` (macOS, Linux): downloaded package tarballs, packages that no environment uses, the index cache, and logs.
- `dnf` (Linux): downloaded package files. Runs with sudo.
- `docker` (macOS, Linux): the build cache, unused images, stopped containers, unused networks, and unused volumes.
- `flatpak` (Linux): runtimes and extensions that no app uses, in your user installation and, with sudo, the system installation.
- `go` (macOS, Linux): the build cache and the module download cache.
- `macports` (macOS): build files, source archives, and inactive ports. Both actions run with sudo.
- `maestro` (macOS, Linux): logs, screenshots, and local recordings of past `maestro test` runs. Skipped while Maestro runs.
- `mise` (macOS, Linux): stale cache files and unused tool versions, or the entire cache with `--dangerous`.
- `npm` (macOS, Linux): unneeded cache data and the npx cache, or the entire cache with `--dangerous`.
- `pacman` (Linux): cached package versions beyond the newest 3, and every cached version of uninstalled packages. Needs `paccache` from pacman-contrib, and runs with sudo.
- `pip` (macOS, Linux): the download and wheel cache, even when pip is no longer installed.
- `pnpm` (macOS, Linux): store packages that no project uses, and registry metadata in formats the installed pnpm can no longer read.
- `podman` (macOS, Linux): unused images, stopped containers, pods, networks, and build cache, and unused volumes with `--dangerous`.
- `pre-commit` (macOS, Linux): unused hook repos, or the entire cache with `--dangerous`.
- `prek` (macOS, Linux): unused hook environments, or the entire cache with `--dangerous`.
- `rip2` (macOS, Linux): the rip2 graveyard of deleted files. Runs only when `rip` on PATH is rip2.
- `thumbnails` (Linux): the desktop file thumbnail cache.
- `trash` (macOS, Linux): everything in the Trash in your home folder. On macOS, the terminal needs Full Disk Access.
- `uv` (macOS, Linux): unused cache entries, or the entire cache with `--dangerous`.
- `xcode` (macOS): build products, device debug symbols, and app archives.
- `yarn` (macOS, Linux): the global package cache, even when yarn is no longer installed.

purger skips a plugin when its tool is not installed, is not running, or does not apply to your platform. Run `purger clean --dry-run` to see the actions that would run, with their risk tiers.

## Install

purger needs Python 3.13 or later.

To install purger with uv, run this command:

```bash
uv tool install purger
```

To install purger with pip, run this command:

```bash
pip install purger
```

Run purger as your normal user. purger refuses to run as root. It asks for sudo only for the actions that need it, such as the `apt` and `macports` actions.

## Quick start

Start with `purger info`. It measures each cache and prints a table with one row for each tool: the size of its cache and a short description. A check in the `Partial purge` column means that a clean removes only part of that size. `purger info` deletes nothing.

```bash
purger info
```

Then run `purger clean`. It prints the same table, asks for confirmation, and runs the cleanups that you approve. At the end, it prints a table with the size of each cache before and after the clean, and the space that was freed.

```bash
purger clean
```

## Commands

Both commands accept plugin names as arguments. Without names, a command uses every plugin.

```bash
purger info uv brew              # measure only the uv and Homebrew caches
purger clean xcode --dry-run     # show what would be cleaned, change nothing
purger clean docker --dangerous  # also offer to remove unused Docker volumes
```

These options apply to both commands:

| Option        | Effect                                                                    |
| ------------- | ------------------------------------------------------------------------- |
| `--dangerous` | Include `dangerous` actions. Without it, purger hides them.               |
| `--json`      | Print exactly one JSON document on stdout instead of tables and messages. |
| `-v`, `-vv`   | Show more detail, such as the output of the tool commands.                |

These options apply to `purger clean` only:

| Option        | Effect                                                                               |
| ------------- | ------------------------------------------------------------------------------------ |
| `--dry-run`   | List the commands and paths that would run. purger asks nothing and changes nothing. |
| `--yes`, `-y` | Skip the single prompt for `safe` actions.                                           |

## Risk tiers

Each cleanup action has one tier. The tier controls how purger asks for confirmation.

| Tier        | Meaning                                                                               | Confirmation                                    |
| ----------- | ------------------------------------------------------------------------------------- | ----------------------------------------------- |
| `safe`      | The tool rebuilds the cache cheaply and automatically.                                | One prompt for all `safe` actions.              |
| `caution`   | The cache can be rebuilt, but the rebuild is slow or a large download.                | One prompt for each action.                     |
| `dangerous` | The action can destroy data that cannot be recreated, or it removes a complete cache. | Shown only with `--dangerous`. One prompt each. |

The `--yes` flag skips only the prompt for `safe` actions. A `caution` or `dangerous` action never runs unless a person answers its prompt.

> **Warning:** Docker `volumes` is a `dangerous` action. It deletes every volume that no container uses, which includes the data of local databases.

## Use purger in scripts and agents

Add `--json` to a command to get exactly one JSON document on stdout. Every document has `"schema_version": 1`, and every size is an integer number of bytes.

```bash
purger info --json
purger clean --json --yes
```

`purger clean --json` never prompts. With `--yes`, it runs the `safe` actions only. It reports each `caution` and `dangerous` action as `declined`. Without a terminal, purger applies the same rules.

The help pages give the full document shape and the status values:

```bash
purger info --help
purger clean --help
```

## Exit codes

Both commands use these exit codes:

| Code  | Meaning                                                                 |
| ----- | ----------------------------------------------------------------------- |
| `0`   | Success. This includes a run where you declined every action.           |
| `1`   | An action that you approved failed. Only `purger clean` uses this code. |
| `2`   | A usage error, such as an unknown plugin name or a run as root.         |
| `130` | You pressed Ctrl-C.                                                     |

## Contributing

To build purger, run the tests, or add a plugin, read [CONTRIBUTING.md](CONTRIBUTING.md).

## License

purger is available under the [MIT License](LICENSE).
