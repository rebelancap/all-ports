#!/usr/bin/env python3
"""
ONE-SHOT migration. Seeds apps-ios.json / apps-visionos.json with the version history
already accumulated by the five per-project sources this repo replaces.

Why it matters: generate.py only knows the LATEST release of each repo. Its version
history survives because load_existing() reads it back out of the file it wrote last
time. Start this repo with empty sources and every app permanently loses its older
versions (q2repro would drop from five entries to one). So: run this ONCE, commit the
result, and let generate.py take over from there.

Safe to delete once the migration is committed.

    python3 migrate-from-legacy.py           # write the seeded sources
    python3 migrate-from-legacy.py --dry-run # just report what it would carry over
"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import PLATFORMS, app_entry, load_config, here  # noqa: E402

DEV = os.path.expanduser("~/dev")
LEGACY = [
    f"{DEV}/quake-ports-source",
    f"{DEV}/harbourmasters/harbourmasters-ports-source",
    f"{DEV}/sm64coopdx-ios/sidestore",
    f"{DEV}/dusklight/sidestore",
    f"{DEV}/apotris-ios/sidestore",
]


def legacy_versions():
    """{platform: {bundleIdentifier: versions[]}} across every legacy source."""
    found = {k: {} for k in PLATFORMS}
    for base in LEGACY:
        for kind in PLATFORMS:
            path = os.path.join(base, f"apps-{kind}.json")
            try:
                with open(path) as f:
                    doc = json.load(f)
            except FileNotFoundError:
                continue
            for a in doc.get("apps", []):
                bid, versions = a["bundleIdentifier"], a.get("versions", [])
                if not versions:
                    continue
                if bid in found[kind]:
                    print(f"  note: {bid} ({kind}) appears in more than one legacy "
                          f"source; keeping the longer history")
                    if len(found[kind][bid]) >= len(versions):
                        continue
                found[kind][bid] = versions
    return found


def main():
    dry = "--dry-run" in sys.argv
    sources, apps, news = load_config()
    legacy = legacy_versions()

    for kind in PLATFORMS:
        entries, carried, fresh = [], 0, []
        for cfg in apps:
            if kind not in cfg["platforms"]:
                continue
            versions = legacy[kind].get(cfg["bundleIdentifier"])
            if versions:
                entries.append(app_entry(cfg, versions))
                carried += 1
                print(f"  {kind:9} {cfg['slug']:16} carried {len(versions)} version(s): "
                      f"{', '.join(v['version'] for v in versions)}")
            else:
                # No legacy history — generate.py will populate it from the latest release.
                fresh.append(cfg["slug"])

        if fresh:
            print(f"  {kind:9} no legacy history for: {', '.join(fresh)} "
                  f"(generate.py will fill these in)")

        out = here(f"apps-{kind}.json")
        if dry:
            print(f"  [dry-run] would write {os.path.basename(out)} with {carried} app(s)\n")
            continue
        doc = dict(sources[kind])
        doc["apps"] = entries
        doc["news"] = news
        with open(out, "w") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"  wrote {os.path.basename(out)} — {carried} app(s) seeded\n")

    print("Now run: python3 generate.py")


if __name__ == "__main__":
    sys.exit(main())
