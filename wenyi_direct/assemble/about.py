"""生成书末“关于此翻译”说明页。"""

from __future__ import annotations

import os
import posixpath
import zipfile

from bs4 import BeautifulSoup

ABOUT_TITLE = "关于此翻译"
ABOUT_FILENAME = "kamyi-about.xhtml"

def about_xhtml(lang: str) -> bytes:
    """返回可独立加入 EPUB spine 的 XHTML 页面。"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">
<head>
  <title>{ABOUT_TITLE}</title>
  <style>
    html, body {{ writing-mode: horizontal-tb; direction: ltr; }}
    body {{ margin: 8% 8%; font-family: serif; line-height: 1.65; }}
    .tn-about {{ max-width: 42em; margin: 0 auto; }}
    h1 {{ margin: 0 0 1.8em; text-align: center; font-size: 1.7em; }}
    p {{ margin: 0 0 1.5em; text-align: justify; }}
    .tn-about-lead, .tn-about-closing {{ text-align: center; }}
  </style>
</head>
<body>
  <section class="tn-about">
    <h1>{ABOUT_TITLE}</h1>
    <p class="tn-about-lead">本书由 <strong>Kamyi</strong> 生成。</p>
    <p>该工具先直接翻译完整章节，再依次进行事实核对、纯中文阅读检查和必要的源文验证。译文仅在全部质量门通过后写入正式文本。</p>
    <p>自动翻译仍可能出错；如用于正式传播，请继续进行人工校对。</p>
    <p class="tn-about-closing">感谢阅读。</p>
  </section>
</body>
</html>
""".encode("utf-8")


def rootfile_path(container_xml: bytes) -> str | None:
    """从 EPUB container.xml 读取主 OPF 路径。"""
    try:
        soup = BeautifulSoup(container_xml, "xml")
        rootfile = soup.find("rootfile")
        if rootfile is None:
            return None
        path = rootfile.get("full-path")
        return path if isinstance(path, str) and path else None
    except Exception:
        return None


def unique_about_entry(existing_names: set[str], opf_path: str) -> tuple[str, str]:
    """返回不与原书资源冲突的（zip 内路径, OPF 相对 href）。"""
    opf_dir = posixpath.dirname(opf_path)
    stem, ext = posixpath.splitext(ABOUT_FILENAME)
    suffix = 0
    while True:
        filename = ABOUT_FILENAME if suffix == 0 else f"{stem}-{suffix}{ext}"
        entry = posixpath.join(opf_dir, filename) if opf_dir else filename
        if entry not in existing_names:
            return entry, filename
        suffix += 1


def append_about_to_opf(data: bytes, href: str) -> tuple[bytes, bool]:
    """把说明页加入 OPF manifest/spine，并返回是否成功挂载。"""
    try:
        soup = BeautifulSoup(data, "xml")
        manifest = soup.find("manifest")
        spine = soup.find("spine")
        if manifest is None or spine is None:
            return data, False

        existing_ids: set[str] = set()
        for existing_item in manifest.find_all("item"):
            value = existing_item.get("id")
            if isinstance(value, str):
                existing_ids.add(value)
        item_id = "kamyi-about"
        suffix = 1
        while item_id in existing_ids:
            item_id = f"kamyi-about-{suffix}"
            suffix += 1

        item = soup.new_tag("item")
        item["id"] = item_id
        item["href"] = href
        item["media-type"] = "application/xhtml+xml"
        manifest.append(item)

        itemref = soup.new_tag("itemref")
        itemref["idref"] = item_id
        spine.append(itemref)
        return soup.encode(), True
    except Exception:
        return data, False


def append_about_page(epub_path: str, lang: str) -> bool:
    """对已经生成的 EPUB 做一次原子后处理，将说明页追加到 spine 末尾。"""
    with zipfile.ZipFile(epub_path, "r") as zin:
        try:
            opf_path = rootfile_path(zin.read("META-INF/container.xml"))
        except KeyError:
            return False
        if not opf_path or opf_path not in zin.namelist():
            return False

        infos = zin.infolist()
        entries = {info.filename: zin.read(info.filename) for info in infos}
        about_entry, about_href = unique_about_entry(set(entries), opf_path)
        opf_data, attached = append_about_to_opf(entries[opf_path], about_href)
        if not attached:
            return False
        entries[opf_path] = opf_data

    tmp_path = epub_path + ".about.tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for info in infos:
                data = entries[info.filename]
                if info.filename == "mimetype":
                    zout.writestr(info, data, zipfile.ZIP_STORED)
                else:
                    zout.writestr(info, data)
            zout.writestr(about_entry, about_xhtml(lang))
        os.replace(tmp_path, epub_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return True
