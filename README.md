# purger

purger finds the caches that developer tools leave on your disk, shows how much space each cache uses, and removes the caches that you approve. It uses the cleanup command of each tool when the tool has one. It deletes a cache directory by hand only when no command exists, and only inside your home directory.

Without `--dangerous` and your explicit confirmation, purger never deletes your files, your projects, or data that cannot be rebuilt.

## What purger cleans

purger runs on macOS and Linux. Each supported tool is a plugin, and each plugin has one or more cleanup actions.

- `apt` (Linux): downloaded package files. Both actions run with sudo.
- `brew` (macOS, Linux): old formula versions and the download cache.
- `docker` (macOS, Linux): the build cache, unused images, stopped containers, unused networks, and unused volumes.
- `prek` (macOS, Linux): unused hook environments, or the entire cache with `--dangerous`.
- `uv` (macOS, Linux): unused cache entries, or the entire cache with `--dangerous`.
- `xcode` (macOS): build products, device debug symbols, and app archives.
- And more ...

purger skips a plugin when its tool is not installed, is not running, or does not apply to your platform. Run `purger --help` to see every action with its risk tier.

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

Run purger as your normal user. purger refuses to run as root. It asks for sudo only for the actions that need it, such as the `apt` actions.

## Quick start

Start with `purger info`. It measures each cache and prints a table with the size of each one. It deletes nothing.

```bash
purger info
```

Then run `purger clean`. It prints the same table, asks for confirmation, and runs the actions that you approve. At the end, it shows the space that each plugin freed.

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
