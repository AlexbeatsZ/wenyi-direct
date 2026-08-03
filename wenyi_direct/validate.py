"""Container-level output validation."""

from __future__ import annotations

import posixpath
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import unquote

from lxml import etree


def validate_epub(path: str | Path) -> dict[str, object]:
    epub_path = Path(path)
    seven_zip = shutil.which("7z")
    seven_zip_checked = False
    if seven_zip:
        result = subprocess.run(
            [seven_zip, "t", str(epub_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"7z integrity test failed: {result.stderr or result.stdout}")
        seven_zip_checked = True

    with zipfile.ZipFile(epub_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"EPUB CRC failure: {bad}")
        names = set(archive.namelist())
        container = etree.fromstring(archive.read("META-INF/container.xml"))
        rootfiles = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not rootfiles:
            raise RuntimeError("EPUB container has no OPF rootfile")
        for opf_name in rootfiles:
            if opf_name not in names:
                raise RuntimeError(f"missing OPF rootfile: {opf_name}")
            opf = etree.fromstring(archive.read(opf_name))
            manifest = {
                str(item.get("id")): str(item.get("href"))
                for item in opf.xpath("//*[local-name()='manifest']/*[local-name()='item']")
            }
            for idref in opf.xpath("//*[local-name()='spine']/*[local-name()='itemref']/@idref"):
                if idref not in manifest:
                    raise RuntimeError(f"spine idref {idref!r} missing from manifest")
            opf_dir = posixpath.dirname(opf_name)
            for item_id, href in manifest.items():
                if "://" in href or href.startswith("data:"):
                    continue
                resolved = posixpath.normpath(posixpath.join(opf_dir, unquote(href.split("#", 1)[0])))
                if resolved not in names:
                    raise RuntimeError(
                        f"manifest item {item_id!r} resolves to missing file {resolved!r}"
                    )
    return {"ok": True, "seven_zip_checked": seven_zip_checked, "path": str(epub_path)}
