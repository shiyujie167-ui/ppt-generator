from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import native_company


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (
    ROOT
    / "engine/skills/ppt-master/templates/decks/mt_corporate_blue/exports"
    / "mt_corporate_blue_template_preview.pptx"
)


class NativeCompanySlideNumberTest(unittest.TestCase):
    def test_merge_fills_cover_picture_with_center_crop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cover_image = tmp_path / "cover.png"
            Image.new("RGB", (1600, 900), "#004696").save(cover_image)
            output = tmp_path / "merged.pptx"

            stats = native_company.merge(
                TEMPLATE,
                TEMPLATE,
                output,
                {
                    "title": "Cover image regression test",
                    "cover_image": str(cover_image),
                },
            )

            self.assertTrue(stats["cover_image_filled"])
            self.assertEqual(stats["cover_image_crop"], {"l": 0, "t": 23888, "r": 0, "b": 23888})
            self.assertEqual(
                native_company.verify(output, TEMPLATE, expect_cover_image=True),
                [],
            )

            parts = native_company._read_package(output)
            cover_part = native_company._presentation_slide_parts(parts)[0][1]
            cover_xml = parts[cover_part].decode("utf-8")
            picture = native_company._placeholder_picture(cover_xml, "10")
            self.assertIsNotNone(picture)
            self.assertIsNone(native_company._placeholder_shape(cover_xml, "pic", "10"))
            picture_xml = picture.group(0)
            self.assertIn('<a:srcRect t="23888" b="23888"/>', picture_xml)
            self.assertIn("<a:stretch><a:fillRect/></a:stretch>", picture_xml)

            embed = re.search(
                r'<a:blip\b[^>]*\br:embed="([^"]+)"',
                picture_xml,
            )
            self.assertIsNotNone(embed)
            relationships = {
                rel["id"]: rel
                for rel in native_company._parse_rels(parts[native_company._rels_name(cover_part)])
            }
            relationship = relationships[embed.group(1)]
            self.assertEqual(relationship["type"], native_company._REL_TYPE_IMAGE)
            media_part = native_company._resolve_target(cover_part, relationship["target"])
            self.assertEqual(parts[media_part], cover_image.read_bytes())

            cover_xml = cover_xml.replace(
                '<a:srcRect t="23888" b="23888"/>',
                '<a:srcRect t="1" b="49999"/>',
                1,
            )
            parts[cover_part] = cover_xml.encode("utf-8")
            broken_crop = tmp_path / "broken-crop.pptx"
            native_company._write_package(broken_crop, parts)
            self.assertTrue(any(
                "未按居中 cover 裁切" in problem
                for problem in native_company.verify(
                    broken_crop,
                    TEMPLATE,
                    expect_cover_image=True,
                )
            ))

    def test_merge_rejects_invalid_cover_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            invalid = tmp_path / "not-an-image.png"
            invalid.write_text("not an image", encoding="utf-8")
            with self.assertRaisesRegex(native_company.MergeError, "格式不支持"):
                native_company.merge(
                    TEMPLATE,
                    TEMPLATE,
                    tmp_path / "merged.pptx",
                    {"title": "Invalid cover", "cover_image": str(invalid)},
                )

            corrupt = tmp_path / "corrupt.png"
            Image.new("RGB", (32, 32), "#004696").save(corrupt)
            corrupt_bytes = bytearray(corrupt.read_bytes())
            corrupt_bytes[-1] ^= 0x01
            corrupt.write_bytes(corrupt_bytes)
            with self.assertRaisesRegex(native_company.MergeError, "PNG 校验失败"):
                native_company.merge(
                    TEMPLATE,
                    TEMPLATE,
                    tmp_path / "corrupt-merged.pptx",
                    {"title": "Corrupt cover", "cover_image": str(corrupt)},
                )

    def test_merge_rebuilds_body_page_number_from_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "merged.pptx"
            stats = native_company.merge(
                TEMPLATE,
                TEMPLATE,
                output,
                {"title": "Page number regression test"},
            )

            self.assertEqual(stats["slide_numbers_normalized"], 1)
            self.assertEqual(native_company.verify(output, TEMPLATE), [])

            parts = native_company._read_package(output)
            slides = native_company._presentation_slide_parts(parts)
            style = native_company._template_slide_number_style(
                native_company._read_package(TEMPLATE)
            )
            body = parts[slides[2][1]].decode("utf-8")
            fields = [
                match.group(0)
                for match in native_company._SHAPE_RE.finditer(body)
                if 'type="slidenum"' in match.group(0)
            ]

            self.assertEqual(len(fields), 1)
            self.assertEqual(native_company._shape_box(fields[0]), style.box)
            self.assertEqual(
                native_company._body_pr_attrs(fields[0]),
                native_company._body_pr_attrs(style.prototype),
            )
            self.assertEqual(native_company._shape_text(fields[0]), "3")

    def test_merge_reuses_template_notes_master(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            generated = tmp_path / "generated-with-notes.pptx"
            output = tmp_path / "merged.pptx"
            parts = native_company._read_package(TEMPLATE)

            old_master = "ppt/notesMasters/notesMaster1.xml"
            generated_master = "ppt/notesMasters/notesMaster2.xml"
            parts[generated_master] = parts.pop(old_master)
            parts[native_company._rels_name(generated_master)] = parts.pop(
                native_company._rels_name(old_master)
            )

            presentation_rels = native_company._rels_name("ppt/presentation.xml")
            parts[presentation_rels] = parts[presentation_rels].replace(
                b'Target="notesMasters/notesMaster1.xml"',
                b'Target="notesMasters/notesMaster2.xml"',
            )
            native_notes_rels = native_company._rels_name("ppt/notesSlides/notesSlide1.xml")
            parts[native_notes_rels] = parts[native_notes_rels].replace(
                b'Target="../notesMasters/notesMaster1.xml"',
                b'Target="../notesMasters/notesMaster2.xml"',
            )

            notes_part = "ppt/notesSlides/notesSlide2.xml"
            notes_xml = parts["ppt/notesSlides/notesSlide1.xml"].decode("utf-8")
            notes_xml = notes_xml.replace(
                '<a:p><a:endParaRPr lang="en-US" dirty="0"/></a:p>',
                '<a:p><a:r><a:rPr lang="en-US"/><a:t>Imported speaker note</a:t></a:r></a:p>',
                1,
            )
            parts[notes_part] = notes_xml.encode("utf-8")
            parts[native_company._rels_name(notes_part)] = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<Relationships xmlns="{native_company.REL_NS}">'
                f'<Relationship Id="rId1" Type="{native_company._REL_TYPE_NOTES_MASTER}" '
                'Target="../notesMasters/notesMaster2.xml"/>'
                '<Relationship Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
                'Target="../slides/slide3.xml"/>'
                '</Relationships>'
            ).encode("utf-8")

            body_slide = native_company._presentation_slide_parts(parts)[2][1]
            body_rels_name = native_company._rels_name(body_slide)
            body_rels = parts[body_rels_name].decode("utf-8").replace(
                "</Relationships>",
                f'<Relationship Id="rId2" Type="{native_company._REL_TYPE_NOTES_SLIDE}" '
                'Target="../notesSlides/notesSlide2.xml"/></Relationships>',
            )
            parts[body_rels_name] = body_rels.encode("utf-8")

            content_types = parts[native_company.CT_NAME].decode("utf-8")
            content_types = content_types.replace(
                'PartName="/ppt/notesMasters/notesMaster1.xml"',
                'PartName="/ppt/notesMasters/notesMaster2.xml"',
            ).replace(
                "</Types>",
                '<Override PartName="/ppt/notesSlides/notesSlide2.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"/>'
                "</Types>",
            )
            parts[native_company.CT_NAME] = content_types.encode("utf-8")
            native_company._write_package(generated, parts)

            stats = native_company.merge(
                TEMPLATE,
                generated,
                output,
                {"title": "Notes master regression test"},
            )

            self.assertEqual(stats["notes_master_links_redirected"], 1)
            self.assertEqual(native_company.verify(output, TEMPLATE), [])
            merged = native_company._read_package(output)
            self.assertNotIn("ppt/notesMasters/gen_notesMaster2.xml", merged)
            slides = native_company._presentation_slide_parts(merged)
            imported_notes = native_company._related_parts(
                merged,
                slides[2][1],
                native_company._REL_TYPE_NOTES_SLIDE,
            )
            self.assertEqual(len(imported_notes), 1)
            self.assertIn(b"Imported speaker note", merged[imported_notes[0]])
            self.assertEqual(
                native_company._related_parts(
                    merged,
                    imported_notes[0],
                    native_company._REL_TYPE_NOTES_MASTER,
                ),
                ["ppt/notesMasters/notesMaster1.xml"],
            )


if __name__ == "__main__":
    unittest.main()
