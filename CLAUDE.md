# synko — notes for Claude

Kodi Syncplay addon (`script.service.syncplay`). Distributed as a self-hosted Kodi repository at https://nellrun.github.io/synko/, built and deployed by `.github/workflows/release.yml`.

## Bump `addon.xml` version on every code change

When editing files that ship inside the addon zip — `addon.py`, `syncplay/**`, `resources/**` — bump the patch version on the first `<addon ...>` line of `addon.xml` (e.g. `1.3.1` → `1.3.2`) as part of the same change.

Kodi identifies updates purely by version string. Without a bump, the GitHub Pages deploy succeeds but the user's TV never pulls the new code.

Workflow / docs / gitignore changes don't need a bump.

## Branch

Default branch is `master` (not `main`). The release workflow triggers on push to either, but real activity happens on `master`.
