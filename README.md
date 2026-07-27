# all-ports

Two SideStore/AltStore sources covering every rebelancap game port — one for iPhone &
iPad, one for Apple Vision Pro. Both are regenerated hourly from the latest GitHub
release of each port, so an app updates in SideStore as soon as you publish it.

## Add the sources

| Platform | URL |
| --- | --- |
| iPhone & iPad | `https://raw.githubusercontent.com/rebelancap/all-ports/main/apps-ios.json` |
| Apple Vision Pro | `https://raw.githubusercontent.com/rebelancap/all-ports/main/apps-visionos.json` |

## Adding a port

1. Drop a 1024×1024 icon at `assets/<slug>.png`.
2. Copy `apps/_template.json` to `apps/<slug>.json` and fill it in.

That's it — the filename is the slug, and the slug is how the icon is addressed, so
the two can't drift apart. The next hourly run picks it up. A port with no GitHub
release yet is reported and skipped; it appears on its own the day you ship one.

```jsonc
{
  "name": "Quake",
  "bundleIdentifier": "com.rebelancap.vkquake",  // MUST match the shipped Info.plist
  "repo": "rebelancap/vkQuake-ios",
  "platforms": ["ios", "visionos"],              // only what this port actually ships
  ...
}
```

### `bundleIdentifier` is load-bearing

SideStore matches an installed app to its source entry by bundle id. If the two ever
disagree, updates silently stop being offered — no error, the app just never updates.
Change it only when the app's own bundle id changes, and understand that when it does,
iOS treats it as a brand-new app: separate container, no saves carried over, and the
old install is orphaned rather than upgraded.

### `platforms`

Declare only the platforms a port actually ships. It's not about routing — the
generator already routes by IPA filename — it's about telling a deliberate gap apart
from a broken build:

- **Undeclared** platform → skipped silently. Dusklight and sm64coopdx are visionOS-only
  here because upstream already ships iOS builds; stratagus is iOS-only for now.
- **Declared** platform with no matching IPA in the release → the previous entry is kept
  (so the app never vanishes from the store along with its whole version history) and
  the workflow run goes red.

That second case is the one worth having. Without it, a visionOS build that failed to
upload would quietly delist a shipping app and reset its version history to a single
entry on the next run.

## How a release becomes a source entry

- **iOS IPA** = a release `.ipa` whose name does *not* contain `vision`/`xros`.
- **visionOS IPA** = a release `.ipa` whose name *does* contain `vision` or `xros`.
- **Version** comes from the IPA's `CFBundleShortVersionString`, not the git tag — it
  has to match what the installed app reports or SideStore won't offer the update.
- **Release notes** become the version's description in SideStore, so keep the GitHub
  release body consistent with the `--notes` you pass `stage-ota.sh`. Same build, two
  audiences.
- **Version history** is preserved across runs, and unchanged apps are never re-read.

### Reading the version without downloading the IPA

An IPA is a zip, and a zip's table of contents lives at the *end* of the file. Instead
of pulling a 400 MB archive down to read one small plist, `generate.py` asks the server
for three narrow byte ranges — the tail, the central directory, then `Info.plist`
itself — which is a few hundred KB. If the host won't serve ranges it falls back to a
full download automatically, so nothing breaks if that ever changes.

## Instant refresh on release

The hourly cron is the floor, not the ceiling. To have a port repo trigger a rebuild
the moment it publishes, add this to that repo's release workflow:

```yaml
- name: Refresh the SideStore sources
  run: |
    curl -X POST -H "Accept: application/vnd.github+json" \
      -H "Authorization: Bearer ${{ secrets.ALL_PORTS_DISPATCH }}" \
      https://api.github.com/repos/rebelancap/all-ports/dispatches \
      -d '{"event_type":"app-released"}'
```

`ALL_PORTS_DISPATCH` is a fine-grained PAT with **Contents: read & write** on
`rebelancap/all-ports`.

> GitHub disables scheduled workflows after 60 days of repository inactivity. With a
> dozen ports feeding this repo that won't come up in practice — but if everything goes
> quiet for two months, the cron stops and the only signal is an email.

## Local use

```bash
python3 generate.py          # refresh both sources from the latest releases
```

Bad config (missing icon, duplicate bundle id, unknown platform) fails immediately,
before any network calls, and writes nothing.

## The five sources this replaces

`quake-ports`, `harbourmasters-ports`, and the in-repo `sidestore/` sources in
`sm64coopdx-ios`, `dusklight-ios`, and `apotris-ios`. **Leave all five running.** Their
URLs are baked into every existing install, and a deleted source strands those users
with no update path and no error message. They're self-maintaining; just stop adding
new ports to them. `migrate-from-legacy.py` is the one-shot that carried their version
history into this repo.
