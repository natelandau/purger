# purger

## Never run real cleanups

`purger clean` and the plugin commands (`uv cache prune`, `brew cleanup`, `docker ... prune`, and the others) delete real caches on this machine. Use `purger info`, `purger clean --dry-run`, and the tests. Tests mock every tool command and delete only inside `tmp_path`.

## Checks

- `uv run duty test` and `uv run duty lint`. Both skip work when the tree already passed. A filtered `uv run duty test -k NAME` always runs. Do not clear caches to force a run.
- `ty` checks `tests/` too, so test code needs types that ty accepts.

## Invariants to keep in one place

- Tier gating lives only in `clean_flow.plan_clean`. `--yes`, `--json`, and non-TTY runs must never approve a `caution` or `dangerous` action.
- Every path deletion goes through `safety.delete_path`. Do not delete files anywhere else.
- In `--json` mode stdout carries exactly one JSON document. Warnings go to stderr, and tool output must not reach stdout.

## Plugins

Start a new plugin from `src/purger/plugins/_template.py`, which documents every `Plugin` and `Action` field. A new field on either class must be documented in the template, and `tests/plugins/test_template.py` fails until it is. Discovery skips modules whose names start with `_`.

Each plugin gets its own test file, `tests/plugins/test_<module>.py`. Never add a plugin's tests to another plugin's file or to a shared file. Only rules that every plugin must follow go in `tests/plugins/test_definitions.py`. Core module tests stay in `tests/unit/`.

## Public contracts

The JSON document shape (`schema_version: 1`), the status values, the exit codes, and the CLI flags are public. Removing, renaming, or changing the meaning or type of any of them is a breaking change. Adding a new JSON field, status value, or flag is never breaking.

## nclutils

Import every nclutils symbol except `pp` from its submodule (`from nclutils.sh import run_command`). `pp.step()` cannot nest, and prompts must not run inside it.
