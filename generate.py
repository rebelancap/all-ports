#!/usr/bin/env python3
"""
Regenerate apps-ios.json and apps-visionos.json (SideStore/AltStore sources) from the
LATEST GitHub release of every port listed in apps/*.json.

Layout
------
  sources.json      the two source headers (ios / visionos)
  apps/<slug>.json  one file per port; the filename IS the slug
  assets/<slug>.png the app icon, addressed by that same slug
  news.json         optional; copied verbatim into both sources

Rules
-----
- The iOS IPA      = a release .ipa whose name does NOT contain 'vision'/'xros'.
- The visionOS IPA = a release .ipa whose name DOES contain 'vision' or 'xros'.
- A port only appears in the sources for the platforms it declares. A declared
  platform whose IPA is missing is an ERROR (the previous entry is kept so the app
  never silently vanishes with its version history); an undeclared platform is
  simply skipped, no noise.
- The version is read from the IPA's CFBundleShortVersionString (it must match what
  the installed app reports, or SideStore won't offer the update). That read is done
  with HTTP range requests — a few hundred KB instead of the whole IPA — falling back
  to a full download if the server won't serve ranges.
- Existing version history is preserved; unchanged apps are left untouched.

Exit codes: 1 = bad config (nothing written). 0 = sources written; if any declared
platform was missing an IPA, PROBLEMS_FILE is written and the workflow fails the job
*after* committing the still-valid sources.

Stdlib only (works on ubuntu-latest). Uses GITHUB_TOKEN if present for rate limits.
"""
import glob, io, json, os, plistlib, struct, sys, urllib.request, zipfile, zlib

GH = "https://api.github.com"
UA = "all-ports-source"
TOKEN = os.environ.get("GITHUB_TOKEN")
HERE = os.path.dirname(os.path.abspath(__file__))
ICON_BASE = "https://raw.githubusercontent.com/rebelancap/all-ports/main/assets"
PLATFORMS = ("ios", "visionos")
PROBLEMS_FILE = os.path.join(HERE, "problems.txt")
FULL_DOWNLOAD_UNDER = 2 * 1024 * 1024  # ranges aren't worth the round-trips below this

problems = []


def problem(msg):
    """Record a non-fatal defect: annotate it in the Actions log and fail the job later."""
    problems.append(msg)
    print(f"::error::{msg}")


def here(*p):
    return os.path.join(HERE, *p)


# ---------------------------------------------------------------- config loading

def load_config():
    with open(here("sources.json")) as f:
        sources = json.load(f)
    for kind in PLATFORMS:
        if kind not in sources:
            sys.exit(f"FATAL: sources.json is missing the '{kind}' source header")

    apps = []
    for path in sorted(glob.glob(here("apps", "*.json"))):
        slug = os.path.splitext(os.path.basename(path))[0]
        if slug.startswith("_"):  # _template.json and friends
            continue
        with open(path) as f:
            cfg = json.load(f)
        cfg["slug"] = slug
        apps.append(cfg)
    if not apps:
        sys.exit("FATAL: no app configs found in apps/*.json")

    # Validate everything up front, before touching the network.
    errs, seen = [], {}
    for cfg in apps:
        slug = cfg["slug"]
        for field in ("name", "bundleIdentifier", "repo", "platforms"):
            if not cfg.get(field):
                errs.append(f"apps/{slug}.json: missing required field '{field}'")
        for kind in cfg.get("platforms", []):
            if kind not in PLATFORMS:
                errs.append(f"apps/{slug}.json: unknown platform '{kind}' (use ios / visionos)")
        if not os.path.exists(here("assets", f"{slug}.png")):
            errs.append(f"apps/{slug}.json: missing assets/{slug}.png "
                        f"(the icon is addressed by the config's filename)")
        bid = cfg.get("bundleIdentifier")
        if bid in seen:
            errs.append(f"apps/{slug}.json: bundleIdentifier '{bid}' already used by "
                        f"apps/{seen[bid]}.json — SideStore keys apps by bundle id")
        seen[bid] = slug

    # SideStore renders "apps" in array order, so this list IS the shelf order.
    # Anything unlisted sorts to the end alphabetically — adding a port never
    # breaks the build, it just lands at the bottom until you place it.
    order = sources.get("order", [])
    known = {a["slug"] for a in apps}
    for slug in order:
        if slug not in known:
            errs.append(f"sources.json: order lists '{slug}', which has no apps/{slug}.json")
    if len(order) != len(set(order)):
        errs.append("sources.json: order contains a duplicate slug")

    if errs:
        sys.exit("FATAL: bad config\n  " + "\n  ".join(errs))

    rank = {slug: i for i, slug in enumerate(order)}
    apps.sort(key=lambda c: (rank.get(c["slug"], len(order)), c["slug"]))

    news = []
    if os.path.exists(here("news.json")):
        with open(here("news.json")) as f:
            news = json.load(f)

    return sources, apps, news


# ------------------------------------------------------------------ github api

def gh(url):
    hdr = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if TOKEN:
        hdr["Authorization"] = f"Bearer {TOKEN}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r:
        return json.load(r)


def latest_release(repo):
    try:
        return gh(f"{GH}/repos/{repo}/releases/latest")
    except Exception as e:
        print(f"  [{repo}] no latest release ({e})")
        return None


def pick_asset(rel, want_vision):
    for a in rel.get("assets", []):
        n = a["name"].lower()
        if not n.endswith(".ipa"):
            continue
        is_vision = ("vision" in n) or ("xros" in n)
        if is_vision == want_vision:
            return a
    return None


# --------------------------------------------- reading a version out of an IPA
# An IPA is a zip, and a zip's table of contents sits at the END of the file. So
# rather than pull down a 400 MB archive to read one small plist, we ask the server
# for three narrow byte ranges: the tail (to find the central directory), the
# central directory itself (to locate Info.plist), and the plist's own bytes.

class NoRanges(Exception):
    """The server ignored our Range header, or the zip needs the slow path."""


def _open(url, extra=None):
    hdr = {"User-Agent": UA}
    if extra:
        hdr.update(extra)
    return urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=120)


def fetch_range(url, start, end):
    """Bytes [start, end] inclusive. Raises NoRanges if the server won't do it."""
    with _open(url, {"Range": f"bytes={start}-{end}"}) as r:
        if r.status != 206:
            raise NoRanges(f"HTTP {r.status} for a range request (expected 206)")
        return r.read()


def remote_size(url):
    with _open(url, {"Range": "bytes=0-0"}) as r:
        if r.status != 206:
            raise NoRanges(f"HTTP {r.status} for a range request (expected 206)")
        # Content-Range: bytes 0-0/12345678
        cr = r.headers.get("Content-Range") or ""
        if "/" not in cr:
            raise NoRanges("no Content-Range in the response")
        return int(cr.rsplit("/", 1)[1])


def _central_directory(url, size):
    tail_len = min(size, 65557 + 64)  # 22-byte EOCD + up to a 65535-byte comment
    tail = fetch_range(url, size - tail_len, size - 1)
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0:
        raise NoRanges("no end-of-central-directory record found")
    cd_size, cd_off = struct.unpack_from("<II", tail, pos + 12)
    if 0xFFFFFFFF in (cd_size, cd_off):
        raise NoRanges("zip64 archive")  # rare for an IPA; just take the slow path
    return fetch_range(url, cd_off, cd_off + cd_size - 1)


def _find_info_plist(cd):
    """Locate Payload/<one>.app/Info.plist and return (local_offset, method, csize)."""
    i = 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        method, = struct.unpack_from("<H", cd, i + 10)
        csize, = struct.unpack_from("<I", cd, i + 20)
        nlen, elen, clen = struct.unpack_from("<HHH", cd, i + 28)
        loff, = struct.unpack_from("<I", cd, i + 42)
        name = cd[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        if (name.startswith("Payload/") and name.endswith(".app/Info.plist")
                and name.count("/") == 2):
            return loff, method, csize
        i += 46 + nlen + elen + clen
    raise NoRanges("no Payload/*.app/Info.plist in the central directory")


def _read_member(url, loff, method, csize):
    head = fetch_range(url, loff, loff + 29)
    if head[:4] != b"PK\x03\x04":
        raise NoRanges("bad local file header")
    nlen, elen = struct.unpack_from("<HH", head, 26)  # local extra != central extra
    start = loff + 30 + nlen + elen
    raw = fetch_range(url, start, start + csize - 1)
    if method == 0:
        return raw
    if method == 8:
        return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw)
    raise NoRanges(f"unsupported compression method {method}")


def _version_via_ranges(url):
    size = remote_size(url)
    if size < FULL_DOWNLOAD_UNDER:
        raise NoRanges("small file")
    cd = _central_directory(url, size)
    plist = _read_member(url, *_find_info_plist(cd))
    return plistlib.loads(plist).get("CFBundleShortVersionString"), size


def _version_via_full_download(url):
    with _open(url) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    for n in z.namelist():
        if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2:
            v = plistlib.loads(z.read(n)).get("CFBundleShortVersionString")
            if v:
                return v
    return None


def ipa_version(url, fallback):
    """CFBundleShortVersionString from the IPA, by range request where possible."""
    try:
        v, size = _version_via_ranges(url)
        if v:
            print(f"    read version {v} via range requests (IPA is {size/1e6:.0f} MB)")
            return v
    except NoRanges as e:
        print(f"    (range read unavailable: {e}; downloading the whole IPA)")
    except Exception as e:
        print(f"    (range read failed: {e}; downloading the whole IPA)")

    try:
        v = _version_via_full_download(url)
        if v:
            return v
    except Exception as e:
        print(f"    (couldn't read IPA version, using tag: {e})")
    return fallback


# ------------------------------------------------------------------- building

def load_existing(path):
    try:
        with open(path) as f:
            return {a["bundleIdentifier"]: a for a in json.load(f).get("apps", [])}
    except Exception:
        return {}


def build_versions(cfg, rel, asset, want_vision, prev):
    url = asset["browser_download_url"]
    prev_versions = (prev or {}).get("versions", [])
    # Unchanged? Keep the previous entry verbatim — no re-read of the IPA.
    if prev_versions and prev_versions[0].get("downloadURL") == url:
        return prev_versions

    tag = rel.get("tag_name", "1.0.0").lstrip("v")
    entry = {
        "version": ipa_version(url, tag),
        "date": rel.get("published_at") or rel.get("created_at"),
        "localizedDescription": (rel.get("body") or "").strip() or "See the release notes on GitHub.",
        "downloadURL": url,
        "size": asset["size"],
        "minOSVersion": cfg["visionosMinOS"] if want_vision else cfg["iosMinOS"],
    }
    # Replace the head if it's the same version re-cut, else prepend.
    if prev_versions and prev_versions[0].get("version") == entry["version"]:
        return [entry] + prev_versions[1:]
    return [entry] + prev_versions


def app_entry(cfg, versions):
    return {
        "name": cfg["name"],
        "bundleIdentifier": cfg["bundleIdentifier"],
        "developerName": cfg.get("developerName", "rebelancap"),
        "subtitle": cfg.get("subtitle", ""),
        "localizedDescription": cfg.get("localizedDescription", ""),
        "iconURL": f"{ICON_BASE}/{cfg['slug']}.png",
        "tintColor": cfg.get("tintColor", "#4C5C96"),
        "category": "games",
        "screenshotURLs": cfg.get("screenshotURLs", []),
        "versions": versions,
    }


def write_source(kind, header, apps, news, out_path):
    src = dict(header)
    src["apps"] = apps
    src["news"] = news
    with open(out_path, "w") as f:
        json.dump(src, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {os.path.basename(out_path)} ({len(apps)} app(s))")


def main():
    sources, apps, news = load_config()

    prev = {k: load_existing(here(f"apps-{k}.json")) for k in PLATFORMS}
    out = {k: [] for k in PLATFORMS}

    for cfg in apps:
        slug, bid, declared = cfg["slug"], cfg["bundleIdentifier"], cfg["platforms"]
        print(f"[{slug}] {cfg['repo']} ({', '.join(declared)})")

        rel = latest_release(cfg["repo"])
        if not rel:
            # A transient API hiccup must never drop an app from the source.
            for kind in declared:
                if bid in prev[kind]:
                    out[kind].append(prev[kind][bid])
                    print(f"  {kind:9}-> kept previous entry (release lookup failed)")
            continue

        for kind in declared:
            want_vision = kind == "visionos"
            asset = pick_asset(rel, want_vision)
            if not asset:
                # Declared but absent: a build or upload went wrong. Keep what we had
                # rather than delisting the app and discarding its version history.
                if bid in prev[kind]:
                    out[kind].append(prev[kind][bid])
                    problem(f"{slug}: no {kind} .ipa in {cfg['repo']} release "
                            f"{rel.get('tag_name')} — kept the previous entry. Check the "
                            f"release assets, or drop '{kind}' from apps/{slug}.json.")
                else:
                    problem(f"{slug}: declares {kind} but {cfg['repo']} release "
                            f"{rel.get('tag_name')} has no {kind} .ipa and there is no "
                            f"previous entry — the app is absent from the {kind} source.")
                continue

            versions = build_versions(cfg, rel, asset, want_vision, prev[kind].get(bid))
            out[kind].append(app_entry(cfg, versions))
            print(f"  {kind:9}-> {versions[0]['version']} ({versions[0]['size']/1e6:.0f} MB)")

    for kind in PLATFORMS:
        write_source(kind, sources[kind], out[kind], news, here(f"apps-{kind}.json"))

    if problems:
        with open(PROBLEMS_FILE, "w") as f:
            f.write("\n".join(problems) + "\n")
        print(f"\n{len(problems)} problem(s) — sources were still written.")
    elif os.path.exists(PROBLEMS_FILE):
        os.remove(PROBLEMS_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
