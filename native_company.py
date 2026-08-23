"""公司模板原生合并。

以公司模板原稿 PPTX 为底(保留全部原生母版/版式/媒体):
1. 第 1 页封面:只替换 ctrTitle / subTitle 占位符文字;
   若 native_fill.cover_image 存在,则按原稿顶部图片占位框做居中 cover 裁切并填入;
2. 第 2 页目录:只替换标题占位符与原生表格第 2 列的条目文字;
3. 删除原稿第 3 页(空白示例页);
4. 把生成侧 PPTX 的第 3 页起(正文页)连同其版式/母版/主题/媒体整链导入,
   导入件全部加 gen_ 前缀避免冲突;
5. 用原稿母版的页码框、锚点、字体与颜色替换正文页静态页码,保留动态编号。

纯标准库实现(zipfile + 字符串/最小 XML 处理),不依赖 Codex/Node/python-pptx。
可独立运行:python3 native_company.py <原稿> <生成pptx> <输出> [fill.json]
"""
from __future__ import annotations

import json
import posixpath
import re
import struct
import sys
import uuid
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape, quoteattr

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DML_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
CT_NAME = "[Content_Types].xml"
_REL_TYPE_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
_REL_TYPE_LAYOUT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_REL_TYPE_MASTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
_REL_TYPE_THEME = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
_REL_TYPE_NOTES_MASTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster"
_REL_TYPE_NOTES_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
_REL_TYPE_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_NS = {"p": PML_NS, "a": DML_NS}
_SHAPE_RE = re.compile(r"<p:sp(?:\s[^>]*)?>.*?</p:sp>", re.S)
_PIC_RE = re.compile(r"<p:pic(?:\s[^>]*)?>.*?</p:pic>", re.S)


class MergeError(RuntimeError):
    """合并失败(输入不满足公司模板合并前提)。"""


@dataclass(frozen=True)
class _SlideNumberStyle:
    prototype: str
    box: tuple[int, int, int, int]
    font: str
    color: str


# ── zip 读写 ─────────────────────────────────────────────────────────────


def _read_package(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {info.filename: zf.read(info.filename) for info in zf.infolist() if not info.is_dir()}


def _write_package(path: Path, parts: dict[str, bytes]) -> None:
    ordered = sorted(parts, key=lambda name: (name != CT_NAME, name))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ordered:
            zf.writestr(name, parts[name])


# ── rels 解析 ────────────────────────────────────────────────────────────


def _rels_name(part: str) -> str:
    directory, base = posixpath.split(part)
    return posixpath.join(directory, "_rels", base + ".rels")


def _parse_rels(data: bytes) -> list[dict[str, str]]:
    root = ElementTree.fromstring(data)
    out = []
    for rel in root.findall(f"{{{REL_NS}}}Relationship"):
        out.append({
            "id": rel.get("Id", ""),
            "type": rel.get("Type", ""),
            "target": rel.get("Target", ""),
            "mode": rel.get("TargetMode", "Internal"),
        })
    return out


def _resolve_target(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


# ── 文字填充(字符串级微创手术,不整篇重排 XML)─────────────────────────


def _paragraphs_xml(lines: list[str]) -> str:
    runs = []
    for line in lines:
        runs.append(
            "<a:p><a:r><a:rPr lang=\"zh-CN\" altLang=\"en-US\" dirty=\"0\"/>"
            f"<a:t>{escape(line)}</a:t></a:r></a:p>"
        )
    return "".join(runs)


def _replace_ph_txbody(xml: str, anchor: str, lines: list[str]) -> str:
    """把 anchor(如 type="ctrTitle")所在形状的 txBody 内容替换为 lines。"""
    pos = xml.find(anchor)
    if pos < 0:
        raise MergeError(f"原稿中找不到占位符 {anchor}")
    start = xml.find("<p:txBody>", pos)
    end = xml.find("</p:txBody>", start)
    if start < 0 or end < 0:
        raise MergeError(f"占位符 {anchor} 缺少 txBody")
    inner = "<a:bodyPr/><a:lstStyle/>" + _paragraphs_xml(lines)
    return xml[: start + len("<p:txBody>")] + inner + xml[end:]


def _fill_agenda_table(xml: str, items: list[str]) -> tuple[str, int]:
    """把原生 Agenda 表格每行第 2 个单元格填入 items;多余行保持原样(空)。"""
    tbl_start = xml.find("<a:tbl>")
    tbl_end = xml.find("</a:tbl>")
    if tbl_start < 0 or tbl_end < 0:
        raise MergeError("目录页找不到原生表格")
    tbl = xml[tbl_start:tbl_end]
    rows = re.split(r"(?=<a:tr[ >])", tbl)
    head, body_rows = rows[0], rows[1:]
    filled = 0
    new_rows = []
    for row in body_rows:
        if filled < len(items):
            cells = list(re.finditer(r"<a:txBody>(.*?)</a:txBody>", row, re.S))
            if len(cells) >= 2:
                second = cells[1]
                inner = "<a:bodyPr/><a:lstStyle/>" + _paragraphs_xml([items[filled]])
                row = row[: second.start(1)] + inner + row[second.end(1):]
                filled += 1
        new_rows.append(row)
    return xml[:tbl_start] + head + "".join(new_rows) + xml[tbl_end:], filled


# ── 封面图片填充 ─────────────────────────────────────────────────


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Return JPEG dimensions without pulling an image library into the merger."""
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if marker == 0xDA:  # scan data starts; SOF must already have appeared
            break
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += length
    raise MergeError("封面 JPEG 无法解析尺寸")


def _webp_size(data: bytes) -> tuple[int, int]:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise MergeError("封面 WebP 文件头损坏")
    kind = data[12:16]
    if kind == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if kind == b"VP8 ":
        signature = data.find(b"\x9d\x01\x2a", 20, 40)
        if signature >= 0 and signature + 7 <= len(data):
            width = int.from_bytes(data[signature + 3:signature + 5], "little") & 0x3FFF
            height = int.from_bytes(data[signature + 5:signature + 7], "little") & 0x3FFF
            return width, height
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise MergeError("封面 WebP 无法解析尺寸")


def _image_info(data: bytes) -> tuple[str, str, int, int]:
    """Return (extension, content type, width, height) from image bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        if data[8:12] != b"\x00\x00\x00\r" or data[12:16] != b"IHDR":
            raise MergeError("封面 PNG 缺少有效 IHDR")
        width, height = struct.unpack(">II", data[16:24])
        offset = 8
        saw_iend = False
        while offset + 12 <= len(data):
            length = int.from_bytes(data[offset:offset + 4], "big")
            end = offset + 12 + length
            if end > len(data):
                raise MergeError("封面 PNG 块长度越界")
            chunk_type = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + length]
            expected_crc = int.from_bytes(data[offset + 8 + length:end], "big")
            if zlib.crc32(chunk_type + payload) & 0xFFFFFFFF != expected_crc:
                raise MergeError("封面 PNG 校验失败")
            offset = end
            if chunk_type == b"IEND":
                saw_iend = True
                break
        if not saw_iend:
            raise MergeError("封面 PNG 缺少 IEND")
        result = ("png", "image/png", width, height)
    elif data.startswith(b"\xff\xd8"):
        width, height = _jpeg_size(data)
        result = ("jpeg", "image/jpeg", width, height)
    elif data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        result = ("gif", "image/gif", width, height)
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        result = ("webp", "image/webp", width, height)
    else:
        raise MergeError("封面图片格式不支持(仅 PNG/JPEG/GIF/WebP)")
    if result[2] <= 0 or result[3] <= 0:
        raise MergeError("封面图片尺寸无效")
    return result


def _placeholder_shape(xml: str, ph_type: str, idx: str) -> re.Match[str] | None:
    for match in _SHAPE_RE.finditer(xml):
        shape = _shape_element(match.group(0))
        ph = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
        if ph is not None and ph.get("type") == ph_type and ph.get("idx", "0") == idx:
            return match
    return None


def _placeholder_picture(xml: str, idx: str) -> re.Match[str] | None:
    for match in _PIC_RE.finditer(xml):
        picture = _shape_element(match.group(0))
        ph = picture.find("./p:nvPicPr/p:nvPr/p:ph", _NS)
        if ph is not None and ph.get("type") == "pic" and ph.get("idx", "0") == idx:
            return match
    return None


def _cover_picture_box(parts: dict[str, bytes], cover_part: str) -> tuple[int, int, int, int]:
    layout_part = _related_part(parts, cover_part, _REL_TYPE_LAYOUT)
    layout_xml = parts[layout_part].decode("utf-8")
    placeholder = _placeholder_shape(layout_xml, "pic", "10")
    if placeholder is None:
        raise MergeError("原稿封面版式缺少 pic idx=10 图片占位符")
    box = _shape_box(placeholder.group(0))
    if box is None or box[2] <= 0 or box[3] <= 0:
        raise MergeError("原稿封面图片占位符尺寸无效")
    return box


def _center_cover_crop(
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
) -> dict[str, int]:
    """PowerPoint srcRect percentages (0..100000) for a centered cover crop."""
    source_ratio = source_width / source_height
    target_ratio = target_width / target_height
    crop = {"l": 0, "t": 0, "r": 0, "b": 0}
    if source_ratio > target_ratio:
        amount = round((1 - target_ratio / source_ratio) * 50000)
        crop["l"] = crop["r"] = max(0, min(amount, 49999))
    elif source_ratio < target_ratio:
        amount = round((1 - source_ratio / target_ratio) * 50000)
        crop["t"] = crop["b"] = max(0, min(amount, 49999))
    return crop


def _next_media_part(parts: dict[str, bytes], extension: str) -> str:
    stem = "ppt/media/company_cover"
    candidate = f"{stem}.{extension}"
    counter = 2
    while candidate in parts:
        candidate = f"{stem}_{counter}.{extension}"
        counter += 1
    return candidate


def _next_relationship_id(rels_xml: str) -> str:
    used = set(re.findall(r'\bId="([^"]+)"', rels_xml))
    counter = 1
    while f"rId{counter}" in used:
        counter += 1
    return f"rId{counter}"


def _ensure_content_type(parts: dict[str, bytes], extension: str, content_type: str) -> None:
    xml = parts[CT_NAME].decode("utf-8")
    if not re.search(rf'<Default\b[^>]*\bExtension="{re.escape(extension)}"', xml):
        xml = xml.replace(
            "</Types>",
            f'<Default Extension="{extension}" ContentType="{content_type}"/></Types>',
        )
        parts[CT_NAME] = xml.encode("utf-8")


def _build_cover_picture(
    placeholder_xml: str,
    relationship_id: str,
    image_name: str,
    crop: dict[str, int],
) -> str:
    shape = _shape_element(placeholder_xml)
    c_nv_pr = shape.find("./p:nvSpPr/p:cNvPr", _NS)
    ph = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
    if c_nv_pr is None or ph is None:
        raise MergeError("封面图片占位符结构损坏")
    shape_id = c_nv_pr.get("id", "")
    if not shape_id.isdigit():
        raise MergeError("封面图片占位符缺少有效 id")
    ph_attrs = " ".join(
        f"{name}={quoteattr(value)}"
        for name, value in ph.attrib.items()
        if name in {"type", "sz", "idx"}
    )
    crop_attrs = " ".join(f'{side}="{value}"' for side, value in crop.items() if value)
    src_rect = f"<a:srcRect{(' ' + crop_attrs) if crop_attrs else ''}/>"
    return (
        "<p:pic><p:nvPicPr>"
        f'<p:cNvPr id="{shape_id}" name="Company Cover Image" descr={quoteattr(image_name)}/>'
        '<p:cNvPicPr><a:picLocks noGrp="1" noChangeAspect="1"/></p:cNvPicPr>'
        f"<p:nvPr><p:ph {ph_attrs}/></p:nvPr>"
        "</p:nvPicPr><p:blipFill>"
        f'<a:blip r:embed="{relationship_id}"/>{src_rect}'
        "<a:stretch><a:fillRect/></a:stretch>"
        "</p:blipFill><p:spPr/></p:pic>"
    )


def _fill_cover_picture(parts: dict[str, bytes], cover_part: str, image_path: Path) -> dict[str, object]:
    try:
        image_data = Path(image_path).read_bytes()
    except OSError as exc:
        raise MergeError(f"无法读取封面图片:{image_path}:{exc}") from exc
    extension, content_type, width, height = _image_info(image_data)
    cover_xml = parts[cover_part].decode("utf-8")
    placeholder = _placeholder_shape(cover_xml, "pic", "10")
    if placeholder is None:
        raise MergeError("原稿封面页缺少 pic idx=10 图片占位符")
    box = _cover_picture_box(parts, cover_part)
    crop = _center_cover_crop(width, height, box[2], box[3])

    rels_name = _rels_name(cover_part)
    rels_xml = parts[rels_name].decode("utf-8")
    relationship_id = _next_relationship_id(rels_xml)
    media_part = _next_media_part(parts, extension)
    target = posixpath.relpath(media_part, posixpath.dirname(cover_part))
    relationship = (
        f'<Relationship Id="{relationship_id}" Type="{_REL_TYPE_IMAGE}" Target="{target}"/>'
    )
    rels_xml = rels_xml.replace("</Relationships>", relationship + "</Relationships>")
    picture = _build_cover_picture(
        placeholder.group(0), relationship_id, Path(image_path).name, crop
    )
    cover_xml = cover_xml[:placeholder.start()] + picture + cover_xml[placeholder.end():]

    parts[cover_part] = cover_xml.encode("utf-8")
    parts[rels_name] = rels_xml.encode("utf-8")
    parts[media_part] = image_data
    _ensure_content_type(parts, extension, content_type)
    return {
        "media_part": media_part,
        "source_width": width,
        "source_height": height,
        "crop": crop,
    }


# ── presentation.xml 编辑 ────────────────────────────────────────────────


def _presentation_slide_parts(parts: dict[str, bytes]) -> list[tuple[str, str]]:
    """按 sldIdLst 顺序返回 [(rId, slide part 名)]。"""
    rels = {r["id"]: r for r in _parse_rels(parts[_rels_name("ppt/presentation.xml")])}
    pres = parts["ppt/presentation.xml"].decode("utf-8")
    out = []
    for rid in re.findall(r"<p:sldId [^>]*r:id=\"([^\"]+)\"", pres):
        rel = rels.get(rid)
        if rel is None:
            raise MergeError(f"presentation.xml 引用了不存在的关系 {rid}")
        out.append((rid, _resolve_target("ppt/presentation.xml", rel["target"])))
    return out


def _remove_relationship(rels_xml: str, rid: str) -> str:
    return re.sub(rf"<Relationship [^>]*Id=\"{re.escape(rid)}\"[^>]*/>", "", rels_xml, count=1)


def _remove_ct_override(ct_xml: str, part: str) -> str:
    return re.sub(rf"<Override [^>]*PartName=\"/{re.escape(part)}\"[^>]*/>", "", ct_xml, count=1)


# ── 导入生成侧部件 ───────────────────────────────────────────────────────


def _collect_import_graph(
    gen: dict[str, bytes],
    seeds: list[str],
    *,
    blocked_relationship_types: set[str] | None = None,
) -> set[str]:
    blocked_relationship_types = blocked_relationship_types or set()
    seen: set[str] = set()
    queue = list(seeds)
    while queue:
        part = queue.pop()
        if part in seen or part not in gen:
            continue
        seen.add(part)
        rels = gen.get(_rels_name(part))
        if rels is None:
            continue
        for rel in _parse_rels(rels):
            if rel["mode"] == "External" or rel["type"] in blocked_relationship_types:
                continue
            target = _resolve_target(part, rel["target"])
            if target not in seen:
                queue.append(target)
    return seen


def _renamed(part: str) -> str:
    directory, base = posixpath.split(part)
    return posixpath.join(directory, f"gen_{base}")


def _rewrite_rels(
    data: bytes,
    source_part: str,
    rename: dict[str, str],
    redirects: dict[str, str] | None = None,
) -> bytes:
    xml = data.decode("utf-8")
    redirects = redirects or {}
    destination_source = rename.get(source_part, source_part)

    def sub(match: re.Match[str]) -> str:
        target = match.group(1)
        resolved = _resolve_target(source_part, target)
        renamed = redirects.get(resolved) or rename.get(resolved)
        if renamed is None:
            return match.group(0)
        rel_target = posixpath.relpath(renamed, posixpath.dirname(destination_source))
        return f'Target="{rel_target}"'

    return re.sub(r'Target="([^"]+)"', sub, xml).encode("utf-8")


def _ct_maps(ct_xml: str) -> tuple[dict[str, str], dict[str, str]]:
    defaults = dict(re.findall(r'<Default Extension="([^"]+)" ContentType="([^"]+)"/>', ct_xml))
    overrides = dict(re.findall(r'<Override PartName="([^"]+)" ContentType="([^"]+)"/>', ct_xml))
    return defaults, overrides


# ── 原稿页码契约 ─────────────────────────────────────────────────────────


def _related_part(parts: dict[str, bytes], source_part: str, rel_type: str) -> str:
    matches = _related_parts(parts, source_part, rel_type)
    if len(matches) != 1:
        raise MergeError(f"{source_part} 的 {rel_type.rsplit('/', 1)[-1]} 关系数量异常:{len(matches)}")
    return matches[0]


def _related_parts(parts: dict[str, bytes], source_part: str, rel_type: str) -> list[str]:
    rels = parts.get(_rels_name(source_part))
    if rels is None:
        return []
    return [
        _resolve_target(source_part, rel["target"])
        for rel in _parse_rels(rels)
        if rel["type"] == rel_type and rel["mode"] != "External"
    ]


def _shape_element(shape_xml: str) -> ElementTree.Element:
    extra_prefixes = sorted(
        prefix
        for prefix in set(re.findall(r"\b([A-Za-z_][\w.-]*):[A-Za-z_]", shape_xml))
        if prefix not in {"p", "a", "xml", "xmlns"}
    )
    extra_namespaces = "".join(
        f' xmlns:{prefix}="urn:ppt-web:extension:{prefix}"'
        for prefix in extra_prefixes
    )
    wrapper = (
        f'<root xmlns:p="{PML_NS}" xmlns:a="{DML_NS}"{extra_namespaces}>'
        f"{shape_xml}</root>"
    )
    try:
        return ElementTree.fromstring(wrapper)[0]
    except ElementTree.ParseError as exc:
        raise MergeError(f"页码形状 XML 无法解析:{exc}") from exc


def _shape_box(shape_xml: str) -> tuple[int, int, int, int] | None:
    shape = _shape_element(shape_xml)
    off = shape.find("./p:spPr/a:xfrm/a:off", _NS)
    ext = shape.find("./p:spPr/a:xfrm/a:ext", _NS)
    if off is None or ext is None:
        return None
    try:
        return (
            int(off.get("x", "")),
            int(off.get("y", "")),
            int(ext.get("cx", "")),
            int(ext.get("cy", "")),
        )
    except ValueError:
        return None


def _slide_number_block(xml: str) -> str | None:
    for match in _SHAPE_RE.finditer(xml):
        shape = _shape_element(match.group(0))
        ph = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
        if ph is not None and ph.get("type") == "sldNum":
            return match.group(0)
    return None


def _theme_latin_font(parts: dict[str, bytes], master_part: str) -> str:
    try:
        theme_part = _related_part(parts, master_part, _REL_TYPE_THEME)
        root = ElementTree.fromstring(parts[theme_part])
    except (KeyError, MergeError, ElementTree.ParseError):
        return ""
    latin = root.find(".//a:fontScheme/a:minorFont/a:latin", _NS)
    return (latin.get("typeface", "").strip() if latin is not None else "")


def _theme_color(parts: dict[str, bytes], master_part: str, scheme: str) -> str:
    try:
        master = ElementTree.fromstring(parts[master_part])
        theme_part = _related_part(parts, master_part, _REL_TYPE_THEME)
        theme = ElementTree.fromstring(parts[theme_part])
    except (KeyError, MergeError, ElementTree.ParseError):
        return ""
    color_map = master.find("./p:clrMap", _NS)
    theme_slot = color_map.get(scheme, scheme) if color_map is not None else scheme
    slot = theme.find(f".//a:clrScheme/a:{theme_slot}", _NS)
    if slot is None:
        return ""
    srgb = slot.find("a:srgbClr", _NS)
    if srgb is not None:
        return srgb.get("val", "").upper()
    system = slot.find("a:sysClr", _NS)
    return (system.get("lastClr", "").upper() if system is not None else "")


def _template_slide_number_style(
    parts: dict[str, bytes],
    slides: list[tuple[str, str]] | None = None,
) -> _SlideNumberStyle:
    slides = slides or _presentation_slide_parts(parts)
    if not slides:
        raise MergeError("公司模板没有可用于解析页码的幻灯片")
    reference_slide = slides[1][1] if len(slides) > 1 else slides[0][1]
    layout_part = _related_part(parts, reference_slide, _REL_TYPE_LAYOUT)
    master_part = _related_part(parts, layout_part, _REL_TYPE_MASTER)

    slide_block = _slide_number_block(parts[reference_slide].decode("utf-8"))
    layout_block = _slide_number_block(parts[layout_part].decode("utf-8"))
    master_block = _slide_number_block(parts[master_part].decode("utf-8"))
    if master_block is None:
        raise MergeError("公司模板母版缺少 sldNum 页码占位符")

    box = next(
        (
            candidate_box
            for block in (slide_block, layout_block, master_block)
            if block is not None
            if (candidate_box := _shape_box(block)) is not None
        ),
        None,
    )
    if box is None:
        raise MergeError("公司模板页码占位符缺少有效位置")

    master_shape = _shape_element(master_block)
    body_pr = master_shape.find("./p:txBody/a:bodyPr", _NS)
    if body_pr is None or master_shape.find("./p:txBody/a:lstStyle", _NS) is None:
        raise MergeError("公司模板母版页码缺少文本框样式")
    scheme_node = master_shape.find(".//a:defRPr/a:solidFill/a:schemeClr", _NS)
    scheme = scheme_node.get("val", "") if scheme_node is not None else ""
    return _SlideNumberStyle(
        prototype=master_block,
        box=box,
        font=_theme_latin_font(parts, master_part),
        color=_theme_color(parts, master_part, scheme) if scheme else "",
    )


def _replace_shape_box(shape_xml: str, box: tuple[int, int, int, int]) -> str:
    x, y, cx, cy = box

    def replace_xfrm(match: re.Match[str]) -> str:
        xfrm = re.sub(r'<a:off\b[^>]*/>', f'<a:off x="{x}" y="{y}"/>', match.group(0), count=1)
        return re.sub(r'<a:ext\b[^>]*/>', f'<a:ext cx="{cx}" cy="{cy}"/>', xfrm, count=1)

    updated, count = re.subn(r"<a:xfrm\b[^>]*>.*?</a:xfrm>", replace_xfrm, shape_xml, count=1, flags=re.S)
    if count != 1:
        raise MergeError("公司模板页码原型缺少 xfrm")
    return updated


def _build_slide_number_shape(
    style: _SlideNumberStyle,
    shape_id: int,
    display_number: int,
) -> str:
    shape = _replace_shape_box(style.prototype, style.box)
    shape, count = re.subn(
        r"<p:cNvPr\b[^>]*(?:/>|>.*?</p:cNvPr>)",
        f'<p:cNvPr id="{shape_id}" name="Company Slide Number"/>',
        shape,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise MergeError("公司模板页码原型缺少 cNvPr")
    shape, count = re.subn(
        r"<p:cNvSpPr\b[^>]*(?:/>|>.*?</p:cNvSpPr>)",
        '<p:cNvSpPr txBox="1"/>',
        shape,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise MergeError("公司模板页码原型缺少 cNvSpPr")
    shape, count = re.subn(
        r"<p:nvPr\b[^>]*(?:/>|>.*?</p:nvPr>)",
        "<p:nvPr/>",
        shape,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise MergeError("公司模板页码原型缺少 nvPr")

    if "<a:noFill" not in shape:
        shape = shape.replace("</p:spPr>", "<a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>", 1)

    field_guid = "{" + str(uuid.uuid4()).upper() + "}"

    def replace_field(match: re.Match[str]) -> str:
        field = re.sub(r'\bid="[^"]*"', f'id="{field_guid}"', match.group(0), count=1)
        field, text_count = re.subn(
            r"(<a:t\b[^>]*>).*?(</a:t>)",
            rf"\g<1>{display_number}\g<2>",
            field,
            count=1,
            flags=re.S,
        )
        if text_count != 1:
            raise MergeError("公司模板页码字段缺少缓存文字")
        direct_style = ""
        if style.color:
            direct_style += f'<a:solidFill><a:srgbClr val="{style.color}"/></a:solidFill>'
        if style.font:
            font = quoteattr(style.font)
            direct_style += (
                f"<a:latin typeface={font}/><a:ea typeface={font}/><a:cs typeface={font}/>"
            )
        if direct_style:
            field, style_count = re.subn(
                r"<a:rPr\b([^>]*)/>",
                rf"<a:rPr\1>{direct_style}</a:rPr>",
                field,
                count=1,
            )
            if style_count == 0:
                field = field.replace("</a:rPr>", direct_style + "</a:rPr>", 1)
        return field

    shape, count = re.subn(
        r'<a:fld\b[^>]*\btype="slidenum"[^>]*>.*?</a:fld>',
        replace_field,
        shape,
        count=1,
        flags=re.S,
    )
    if count != 1:
        raise MergeError("公司模板页码原型缺少 slidenum 字段")
    return shape


def _boxes_overlap(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return max(ax, bx) < min(ax + aw, bx + bw) and max(ay, by) < min(ay + ah, by + bh)


def _shape_text(shape_xml: str) -> str:
    shape = _shape_element(shape_xml)
    return "".join(node.text or "" for node in shape.findall(".//a:t", _NS)).strip()


def _is_existing_slide_number(shape_xml: str, box: tuple[int, int, int, int]) -> bool:
    shape = _shape_element(shape_xml)
    ph = shape.find("./p:nvSpPr/p:nvPr/p:ph", _NS)
    if ph is not None and ph.get("type") == "sldNum":
        return True
    if shape.find('.//a:fld[@type="slidenum"]', _NS) is not None:
        return True
    shape_box = _shape_box(shape_xml)
    return bool(shape_box and _shape_text(shape_xml).isdigit() and _boxes_overlap(shape_box, box))


def _normalize_slide_number(
    slide_xml: str,
    style: _SlideNumberStyle,
    display_number: int,
) -> tuple[str, int]:
    shape_ids = [int(value) for value in re.findall(r'<p:cNvPr\b[^>]*\bid="(\d+)"', slide_xml)]
    next_shape_id = max(shape_ids or [1]) + 1
    matches = [
        match
        for match in _SHAPE_RE.finditer(slide_xml)
        if _is_existing_slide_number(match.group(0), style.box)
    ]
    for match in reversed(matches):
        slide_xml = slide_xml[:match.start()] + slide_xml[match.end():]
    page_number = _build_slide_number_shape(style, next_shape_id, display_number)
    if "</p:spTree>" not in slide_xml:
        raise MergeError("生成侧正文页缺少 spTree")
    slide_xml = slide_xml.replace("</p:spTree>", page_number + "</p:spTree>", 1)
    return slide_xml, len(matches)


def _first_slide_number(presentation_xml: str) -> int:
    try:
        root = ElementTree.fromstring(presentation_xml)
        return int(root.get("firstSlideNum", "1"))
    except (ElementTree.ParseError, ValueError):
        return 1


def _body_pr_attrs(shape_xml: str) -> dict[str, str]:
    body_pr = _shape_element(shape_xml).find("./p:txBody/a:bodyPr", _NS)
    return dict(body_pr.attrib) if body_pr is not None else {}


# ── 主流程 ───────────────────────────────────────────────────────────────


def merge(template_path: Path, generated_path: Path, output_path: Path, fill: dict) -> dict:
    tpl = _read_package(Path(template_path))
    gen = _read_package(Path(generated_path))

    tpl_slides = _presentation_slide_parts(tpl)
    if len(tpl_slides) < 2:
        raise MergeError("公司模板原稿不足 2 页")
    slide_number_style = _template_slide_number_style(tpl, tpl_slides)

    # 1) 封面文字
    cover_part = tpl_slides[0][1]
    cover = tpl[cover_part].decode("utf-8")
    title = str(fill.get("title") or "").strip()
    if not title:
        raise MergeError("native_fill 缺少 title")
    cover = _replace_ph_txbody(cover, 'type="ctrTitle"', [title])
    sub_lines = [str(x).strip() for x in (fill.get("subtitle"), fill.get("date")) if str(x or "").strip()]
    if sub_lines:
        cover = _replace_ph_txbody(cover, 'type="subTitle"', sub_lines)
    tpl[cover_part] = cover.encode("utf-8")
    cover_picture: dict[str, object] | None = None
    cover_image = str(fill.get("cover_image") or "").strip()
    if cover_image:
        cover_picture = _fill_cover_picture(tpl, cover_part, Path(cover_image))

    # 2) 目录文字
    toc_part = tpl_slides[1][1]
    toc_xml = tpl[toc_part].decode("utf-8")
    toc_items = [str(x).strip() for x in (fill.get("toc") or []) if str(x).strip()]
    toc_title = str(fill.get("toc_title") or "").strip()
    if toc_title:
        toc_xml = _replace_ph_txbody(toc_xml, 'type="title"', [toc_title])
    filled_rows = 0
    if toc_items:
        toc_xml, filled_rows = _fill_agenda_table(toc_xml, toc_items)
    tpl[toc_part] = toc_xml.encode("utf-8")

    # 3) 删除原稿第 3 页起的示例页
    pres_rels_name = _rels_name("ppt/presentation.xml")
    pres = tpl["ppt/presentation.xml"].decode("utf-8")
    pres_rels = tpl[pres_rels_name].decode("utf-8")
    ct = tpl[CT_NAME].decode("utf-8")
    for rid, part in tpl_slides[2:]:
        pres = re.sub(rf"<p:sldId [^>]*r:id=\"{re.escape(rid)}\"/>", "", pres, count=1)
        pres_rels = _remove_relationship(pres_rels, rid)
        ct = _remove_ct_override(ct, part)
        tpl.pop(part, None)
        tpl.pop(_rels_name(part), None)

    # 4) 生成侧正文页(第 3 页起)+ 依赖链导入
    gen_slides = [part for _, part in _presentation_slide_parts(gen)]
    body_slides = gen_slides[2:]
    if not body_slides:
        raise MergeError("生成侧 PPTX 不足 3 页,没有可并入的正文页")
    # 每个 PPTX 只能登记一套 notesMaster。正文页的 notesSlide 可以保留，但必须
    # 统一改接模板已登记的 notesMaster；把生成稿 notesMaster 一并复制进来会被
    # PowerPoint 打开时删除，并触发“发现内容有问题，需要修复”的提示。
    template_notes_master = _related_part(
        tpl,
        "ppt/presentation.xml",
        _REL_TYPE_NOTES_MASTER,
    )
    graph = _collect_import_graph(
        gen,
        body_slides,
        blocked_relationship_types={_REL_TYPE_NOTES_MASTER},
    )
    rename = {part: _renamed(part) for part in graph}
    notes_master_redirects: dict[str, str] = {}
    for part in graph:
        rels = gen.get(_rels_name(part))
        if rels is None:
            continue
        for rel in _parse_rels(rels):
            if rel["mode"] != "External" and rel["type"] == _REL_TYPE_NOTES_MASTER:
                notes_master_redirects[_resolve_target(part, rel["target"])] = template_notes_master

    gen_defaults, gen_overrides = _ct_maps(gen[CT_NAME].decode("utf-8"))
    tpl_defaults, _ = _ct_maps(ct)
    ct_additions: list[str] = []
    for ext, ctype in gen_defaults.items():
        if ext not in tpl_defaults:
            ct_additions.append(f'<Default Extension="{ext}" ContentType="{ctype}"/>')
    for part in sorted(graph):
        new_name = rename[part]
        data = gen[part]
        tpl[new_name] = data
        rels = gen.get(_rels_name(part))
        if rels is not None:
            tpl[_rels_name(new_name)] = _rewrite_rels(
                rels,
                part,
                rename,
                notes_master_redirects,
            )
        ctype = gen_overrides.get(f"/{part}")
        if ctype:
            ct_additions.append(f'<Override PartName="/{new_name}" ContentType="{ctype}"/>')
    if ct_additions:
        ct = ct.replace("</Types>", "".join(ct_additions) + "</Types>")

    # 正文页码不沿用生成侧的紧缩静态文本框；每页改为原稿样式的动态字段。
    first_slide_number = _first_slide_number(pres)
    removed_slide_numbers = 0
    for output_index, part in enumerate(body_slides, start=2):
        imported_part = rename[part]
        slide_xml = tpl[imported_part].decode("utf-8")
        slide_xml, removed = _normalize_slide_number(
            slide_xml,
            slide_number_style,
            first_slide_number + output_index,
        )
        tpl[imported_part] = slide_xml.encode("utf-8")
        removed_slide_numbers += removed

    # 6) 注册导入的母版与正文页
    used_rids = set(re.findall(r'Id="([^"]+)"', pres_rels))

    def next_rid(counter: list[int]) -> str:
        while True:
            counter[0] += 1
            rid = f"rId{counter[0]}"
            if rid not in used_rids:
                used_rids.add(rid)
                return rid

    counter = [100]
    rel_additions: list[str] = []

    # sldMasterId 与 sldLayoutId 共用同一全局 id 空间(ECMA-376 要求全文档唯一),
    # 撞号 PowerPoint 打开时就会提示"修复"。模板实际分布:母版 2147483660、版式
    # 2147483661 起——只按母版 id 取 max+1 正好命中版式 id;生成侧母版自带的版式 id
    # 也可能落进模板区间。故原生 id 一律保留,新母版 id 与导入版式的撞号 id 统一
    # 从并集之外分配。版式 id 只出现在所属母版的 sldLayoutIdLst 里,重编无其他引用。
    imported_masters = sorted(
        rename[p] for p in graph if re.fullmatch(r"ppt/slideMasters/[^/]+\.xml", p))
    taken = {int(x) for x in re.findall(r'<p:sldMasterId id="(\d+)"', pres)}
    for name, data in tpl.items():
        if re.fullmatch(r"ppt/slideMasters/[^/]+\.xml", name) and name not in imported_masters:
            taken.update(
                int(x) for x in re.findall(r'<p:sldLayoutId id="(\d+)"', data.decode("utf-8", "replace")))

    def claim(preferred: int | None = None) -> int:
        if preferred is not None and preferred not in taken:
            taken.add(preferred)
            return preferred
        value = max([*taken, 2147483647]) + 1
        taken.add(value)
        return value

    for name in imported_masters:
        xml = tpl[name].decode("utf-8")
        xml = re.sub(
            r'(<p:sldLayoutId id=")(\d+)(")',
            lambda m: f"{m.group(1)}{claim(int(m.group(2)))}{m.group(3)}",
            xml,
        )
        tpl[name] = xml.encode("utf-8")

    master_entries: list[str] = []
    for name in imported_masters:
        rid = next_rid(counter)
        target = posixpath.relpath(name, "ppt")
        rel_additions.append(f'<Relationship Id="{rid}" Type="{_REL_TYPE_MASTER}" Target="{target}"/>')
        master_entries.append(f'<p:sldMasterId id="{claim()}" r:id="{rid}"/>')
    if master_entries:
        pres = pres.replace("</p:sldMasterIdLst>", "".join(master_entries) + "</p:sldMasterIdLst>")

    slide_ids = [int(x) for x in re.findall(r'<p:sldId id="(\d+)"', pres)]
    next_slide_id = max(slide_ids or [255]) + 1
    slide_entries: list[str] = []
    for part in body_slides:
        rid = next_rid(counter)
        target = posixpath.relpath(rename[part], "ppt")
        rel_additions.append(f'<Relationship Id="{rid}" Type="{_REL_TYPE_SLIDE}" Target="{target}"/>')
        slide_entries.append(f'<p:sldId id="{next_slide_id}" r:id="{rid}"/>')
        next_slide_id += 1
    pres = pres.replace("</p:sldIdLst>", "".join(slide_entries) + "</p:sldIdLst>")
    pres_rels = pres_rels.replace("</Relationships>", "".join(rel_additions) + "</Relationships>")

    tpl["ppt/presentation.xml"] = pres.encode("utf-8")
    tpl[pres_rels_name] = pres_rels.encode("utf-8")
    tpl[CT_NAME] = ct.encode("utf-8")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_package(output_path, tpl)
    return {
        "slides": 2 + len(body_slides),
        "body_slides": len(body_slides),
        "imported_parts": len(graph),
        "toc_rows_filled": filled_rows,
        "slide_numbers_normalized": len(body_slides),
        "generated_slide_numbers_removed": removed_slide_numbers,
        "notes_master_links_redirected": len(notes_master_redirects),
        "cover_image_filled": cover_picture is not None,
        "cover_image_crop": cover_picture["crop"] if cover_picture else None,
    }


def _cover_picture_problems(parts: dict[str, bytes], cover_part: str) -> list[str]:
    problems: list[str] = []
    cover_xml = parts.get(cover_part, b"").decode("utf-8", "replace")
    pictures = [match for match in _PIC_RE.finditer(cover_xml) if _placeholder_picture(match.group(0), "10")]
    if len(pictures) != 1:
        return [f"封面 pic idx=10 图片数量异常:{len(pictures)}"]
    picture_xml = pictures[0].group(0)
    embed = re.search(r'<a:blip\b[^>]*\br:embed="([^"]+)"', picture_xml)
    if embed is None:
        problems.append("封面图片缺少 embed 关系")
        return problems
    rels = {
        rel["id"]: rel
        for rel in _parse_rels(parts.get(_rels_name(cover_part), b"<Relationships/>"))
    }
    rel = rels.get(embed.group(1))
    media_part = ""
    if rel is None or rel["type"] != _REL_TYPE_IMAGE:
        problems.append("封面图片 embed 关系无效")
    else:
        media_part = _resolve_target(cover_part, rel["target"])
        if media_part not in parts:
            problems.append("封面图片媒体部件缺失")
    src_rect = re.search(r"<a:srcRect\b([^>]*)/>", picture_xml)
    if src_rect is None:
        problems.append("封面图片缺少自动裁切 srcRect")
    else:
        pairs = re.findall(r'\b([A-Za-z_:][\w:.-]*)="([^"]*)"', src_rect.group(1))
        crop = {"l": 0, "t": 0, "r": 0, "b": 0}
        invalid = any(side not in crop or not re.fullmatch(r"\d+", value) for side, value in pairs)
        if not invalid:
            for side, value in pairs:
                crop[side] = int(value)
            invalid = any(value < 0 or value >= 50000 for value in crop.values())
        if invalid:
            problems.append("封面图片 srcRect 裁切值无效")
        elif media_part and media_part in parts:
            try:
                _, _, width, height = _image_info(parts[media_part])
                box = _cover_picture_box(parts, cover_part)
                expected_crop = _center_cover_crop(width, height, box[2], box[3])
            except MergeError as exc:
                problems.append(f"封面图片无法验证:{exc}")
            else:
                if crop != expected_crop:
                    problems.append(
                        f"封面图片 srcRect 未按居中 cover 裁切:"
                        f"应为 {expected_crop},实为 {crop}"
                    )
    if "<a:stretch><a:fillRect/></a:stretch>" not in picture_xml:
        problems.append("封面图片缺少填满占位框设置")
    return problems


def verify(
    output_path: Path,
    template_path: Path,
    *,
    expect_cover_image: bool = False,
) -> list[str]:
    """母版保真度硬校验;返回问题列表,空列表 = 通过。"""
    problems: list[str] = []
    out = _read_package(Path(output_path))
    tpl = _read_package(Path(template_path))

    native_layouts = {p for p in tpl if re.fullmatch(r"ppt/slideLayouts/[^/]+\.xml", p)}
    kept = {p for p in out if p in native_layouts}
    if kept != native_layouts:
        problems.append(f"原生版式缺失:应有 {len(native_layouts)} 个,实有 {len(kept)} 个")

    template_notes_masters = {
        p for p in tpl if re.fullmatch(r"ppt/notesMasters/[^/]+\.xml", p)
    }
    output_notes_masters = {
        p for p in out if re.fullmatch(r"ppt/notesMasters/[^/]+\.xml", p)
    }
    if output_notes_masters != template_notes_masters:
        problems.append(
            "备注母版集合异常:"
            f"应为 {sorted(template_notes_masters)},实为 {sorted(output_notes_masters)}"
        )

    try:
        slides = _presentation_slide_parts(out)
    except MergeError as exc:
        return [f"presentation.xml 解析失败:{exc}"]
    if len(slides) < 3:
        problems.append(f"成品页数异常:{len(slides)}")
    for index in (0, 1):
        if index >= len(slides):  # 不足 2 页时上面已报问题,别再 IndexError(独立 CLI 会传任意输入)
            break
        part = slides[index][1]
        if part.split("/")[-1].startswith("gen_"):
            problems.append(f"第 {index + 1} 页不是原稿原生页:{part}")
        if "gradFill" in out.get(part, b"").decode("utf-8", "replace"):
            problems.append(f"第 {index + 1} 页出现合成渐变背景,疑似非原生页")
    if slides:
        cover = out.get(slides[0][1], b"").decode("utf-8", "replace")
        if 'type="ctrTitle"' not in cover:
            problems.append("封面缺少原生 ctrTitle 占位符")
        if expect_cover_image:
            problems.extend(_cover_picture_problems(out, slides[0][1]))

    try:
        registered_notes_master = _related_part(
            out,
            "ppt/presentation.xml",
            _REL_TYPE_NOTES_MASTER,
        )
    except MergeError as exc:
        problems.append(f"演示文稿备注母版关系异常:{exc}")
        registered_notes_master = ""
    if registered_notes_master:
        for output_index, (_, part) in enumerate(slides[2:], start=3):
            for notes_part in _related_parts(out, part, _REL_TYPE_NOTES_SLIDE):
                notes_masters = _related_parts(out, notes_part, _REL_TYPE_NOTES_MASTER)
                if notes_masters != [registered_notes_master]:
                    problems.append(
                        f"第 {output_index} 页备注未复用已登记母版:"
                        f"{notes_masters or '无'}"
                    )

    try:
        slide_number_style = _template_slide_number_style(tpl)
    except MergeError as exc:
        problems.append(f"原稿页码契约无法解析:{exc}")
        slide_number_style = None
    if slide_number_style is not None:
        source_body_pr = _body_pr_attrs(slide_number_style.prototype)
        first_slide_number = _first_slide_number(
            out.get("ppt/presentation.xml", b"").decode("utf-8", "replace")
        )
        for output_index, (_, part) in enumerate(slides[2:], start=2):
            slide_xml = out.get(part, b"").decode("utf-8", "replace")
            blocks = [match.group(0) for match in _SHAPE_RE.finditer(slide_xml)]
            fields = [
                block
                for block in blocks
                if _shape_element(block).find('.//a:fld[@type="slidenum"]', _NS) is not None
            ]
            page_number = first_slide_number + output_index
            if len(fields) != 1:
                problems.append(f"第 {output_index + 1} 页动态页码字段数量异常:{len(fields)}")
                continue
            field = fields[0]
            if _shape_box(field) != slide_number_style.box:
                problems.append(f"第 {output_index + 1} 页页码框位置未继承原稿")
            if _body_pr_attrs(field) != source_body_pr:
                problems.append(f"第 {output_index + 1} 页页码锚点/内边距未继承原稿")
            if _shape_text(field) != str(page_number):
                problems.append(f"第 {output_index + 1} 页页码缓存值异常:{_shape_text(field)!r}")
            stale = [
                block
                for block in blocks
                if block != field
                and _shape_element(block).find('.//a:fld[@type="slidenum"]', _NS) is None
                and _is_existing_slide_number(block, slide_number_style.box)
            ]
            if stale:
                problems.append(f"第 {output_index + 1} 页仍有静态/重复页码:{len(stale)}")

    for name, data in out.items():
        if name.endswith(".xml") or name.endswith(".rels"):
            try:
                ElementTree.fromstring(data)
            except ElementTree.ParseError as exc:
                problems.append(f"XML 损坏:{name}({exc})")

    # 母版 id 与版式 id 同处一个全局 id 空间,冲突会让 PowerPoint 提示修复
    ids = [int(x) for x in re.findall(
        r'<p:sldMasterId id="(\d+)"', out.get("ppt/presentation.xml", b"").decode("utf-8", "replace"))]
    for name, data in out.items():
        if re.fullmatch(r"ppt/slideMasters/[^/]+\.xml", name):
            ids.extend(int(x) for x in re.findall(
                r'<p:sldLayoutId id="(\d+)"', data.decode("utf-8", "replace")))
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        problems.append(f"母版/版式 id 冲突(PowerPoint 会要求修复):{duplicated}")
    return problems


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    template, generated, output = (Path(p) for p in sys.argv[1:4])
    fill = {}
    if len(sys.argv) > 4:
        fill = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
    stats = merge(template, generated, output, fill)
    problems = verify(
        output,
        template,
        expect_cover_image=bool(str(fill.get("cover_image") or "").strip()),
    )
    print(json.dumps({"stats": stats, "problems": problems}, ensure_ascii=False, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
