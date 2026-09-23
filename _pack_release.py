#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build the release zip package for the current version.

Layout matches the published convention (verified against
WorkBuddy2API-v1.6.1-win64.zip):

    WorkBuddy2API/
        WorkBuddy2API.exe
        _internal/...
        README.md
        LICENSE

The updater's extract_zip() detects either a top-level folder or files at the
root, so this layout is what in-place updates expect.
"""
import os
import re
import shutil
import sys
import zipfile

PUB = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(PUB, "dist", "WorkBuddy2API")

src = open(os.path.join(PUB, "wb_proxy.py"), encoding="utf-8").read()
version = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src).group(1)

if not os.path.isdir(DIST):
    print("ERROR: %s not found - build first" % DIST)
    sys.exit(1)
if not os.path.isfile(os.path.join(DIST, "WorkBuddy2API.exe")):
    print("ERROR: EXE missing in %s" % DIST)
    sys.exit(1)

# Data items must never ship inside the package.
for bad in ("accounts", "usage", "gateway.json"):
    if os.path.exists(os.path.join(DIST, bad)):
        print("ERROR: %s must not be in the package" % bad)
        sys.exit(1)

out = os.path.join(PUB, "dist", "WorkBuddy2API-v%s-win64.zip" % version)
if os.path.exists(out):
    os.remove(out)

top = "WorkBuddy2API"
count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    # Docs that belong at the package root (same as previous releases).
    for doc in ("README.md", "LICENSE"):
        path = os.path.join(PUB, doc)
        if os.path.isfile(path):
            z.write(path, "%s/%s" % (top, doc))
            count += 1
    # The program itself, preserving the onedir layout.
    for root, dirs, files in os.walk(DIST):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, DIST).replace("\\", "/")
            z.write(full, "%s/%s" % (top, rel))
            count += 1

size = os.path.getsize(out)
print("built : %s" % out)
print("version: %s" % version)
print("entries: %d" % count)
print("size   : %.1f MB (%d bytes)" % (size / 1048576.0, size))
