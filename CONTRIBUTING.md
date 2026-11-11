# Contributing to purger

This guide shows how to set up a development environment, run the checks, add a plugin, and open a pull request.

## Prerequisites

You need these tools:

- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- git

## Set up the environment

Follow these steps to prepare a working copy:

1. Clone the repository and go into its directory.
2. Install the dependencies:

    ```bash
    uv sync
    ```

3. Install the git hooks:

    ```bash
    uv run prek install
    ```

The hooks run on each commit. They format and lint the code, check the types, run the tests, and check the commit message.

## Run the checks

The project uses [duty](https://pawamoy.github.io/duty/) for its tasks. Run each task with `uv run duty TASK`.

| Task     | What it does                                                                     |
| -------- | -------------------------------------------------------------------------------- |
| `test`   | Runs the test suite with coverage.                                               |
| `lint`   | Runs `ruff`, `format`, `ty`, `typos`, and `prek` in sequence.                    |
| `ruff`   | Checks the code with ruff.                                                       |
| `format` | Checks the code format with ruff.                                                |
| `ty`     | Checks the types in `src/` and `tests/`.                                         |
| `typos`  | Checks the spelling.                                                             |
| `prek`   | Runs every git hook on every file.                                               |
| `update` | Upgrades the lock file, the dependencies, and the hook versions.                 |
| `clean`  | Deletes build output, caches, and coverage data.                                 |

Before you open a pull request, run the tests and the linters:

```bash
uv run duty test
uv run duty lint
```

The `test` task and the lint tasks skip their work when the same files already passed. To run a subset of the tests, pass pytest arguments. A filtered run always executes:

```bash
uv run duty test -k safety
```

## Test without risk to your caches

`purger clean` deletes real caches on your machine. During development, use `--dry-run`, `purger info`, or the test suite. The tests never run a real cleanup command.

```bash
uv run purger info
uv run purger clean --dry-run
```

The tests for the core modules live in `tests/unit/`. Each test file matches a module in `src/purger/`.

The tests for the plugins live in `tests/plugins/`. Each plugin has its own test file, named after its module: the tests for `src/purger/plugins/docker.py` are in `tests/plugins/test_docker.py`. Do not put tests for two plugins in one file. The tests that apply a rule to every plugin are in `tests/plugins/test_definitions.py`.

## Add a plugin

Each plugin is one Python file in `src/purger/plugins/`. The file defines a module-level `PLUGIN`, which is a `Plugin` object. purger finds the file without a registration step, and the help page lists the plugin automatically. purger skips each module whose name starts with an underscore.

The file `src/purger/plugins/_template.py` is the reference for plugins. It sets or names each `Plugin` and `Action` field, with a comment that tells what the field does and when to use it. A test fails when a field has no entry in the template, so the template always matches the code.

To add a plugin, do these steps:

1. Copy `_template.py` to a file named after the plugin, with underscores for hyphens (`pre_commit.py` for `pre-commit`).
2. Replace the module docstring with one line that names the tool.
3. Change the plugin fields and the actions to match the tool. Delete each field and action that the tool does not need.
4. If the plugin has two or more command actions, give the plugin its own `size`, `size_cmd`, or `paths`. Their sizes can overlap, so purger cannot add them together. The exception is a plugin where each command action measures exactly what it frees, such as `go`.
5. In `tests/plugins/test_definitions.py`, add the plugin name to `test_discover_plugins_finds_builtin_set` and add each action to `_EXPECTED_ACTIONS`.
6. Put the plugin's own tests in a new file, `tests/plugins/test_<module>.py`. Mock each tool command. A test that runs a real tool command fails.
7. Run `uv run purger info` and `uv run purger --help` to make sure that the plugin appears.

A plugin must not delete anything that the user cannot rebuild, unless the action has the `dangerous` tier. A path action can delete only below your home directory. purger refuses every other path.

If you add a field to `Plugin` or `Action`, document the field in `_template.py`.

## Commit messages

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/). The commit hook rejects a message that does not match.

```text
<type>(<scope>): <subject>
```

- Use one of these types: `build`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `style`, `test`.
- Write the subject in the imperative mood, with a lowercase first letter and no period.
- Keep the subject at 70 characters or less, and each body line at 72 characters or less.

## Pull requests

Open a pull request against `main`. The maintainers squash each pull request into one commit, so the pull request title becomes the commit subject. The title must follow the commit message format.

CI runs the tests on Python 3.13, 3.14, and 3.15, and it runs the linters. CI must pass before a pull request can merge.

## Releases

A maintainer makes a release from `main`:

1. Bump the version and update the changelog:

    ```bash
    uv run cz bump
    ```

2. Push the commit and the new tag:

    ```bash
    git push --follow-tags
    ```

The tag starts the release workflow. The workflow runs the tests, creates a GitHub release, and publishes the package to PyPI.
