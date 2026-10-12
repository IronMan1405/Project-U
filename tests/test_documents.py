import io
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter

import documents
import web_app
from assistant_runtime import AssistantRuntime, TurnResult


def make_pdf(page_texts, compress=False):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(
            f"{4 + index * 2} 0 R".encode("ascii") for index in range(len(page_texts))
        ) + f"] /Count {len(page_texts)} >>".encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, page_text in enumerate(page_texts):
        page_id = 4 + index * 2
        stream_id = page_id + 1
        escaped = page_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        filter_entry = b""
        if compress:
            stream = zlib.compress(stream)
            filter_entry = b" /Filter /FlateDecode"
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>"
                ).encode("ascii"),
                f"<< /Length {len(stream)}".encode("ascii")
                + filter_entry
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
            ]
        )

    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class DocumentExtractionTests(unittest.TestCase):
    def test_extracts_utf8_text_and_preserves_paragraphs(self):
        content = b"First paragraph.\n\nSecond paragraph.\n"
        document = documents.extract_document("notes.txt", content)

        self.assertEqual(document.file_type, "Text")
        self.assertEqual(document.pages[0].text, content.decode("utf-8"))
        self.assertEqual(document.page_count, 1)

    def test_extracts_pdf_text_and_page_boundaries(self):
        payload = make_pdf(["First PDF page has readable text.", "Second page says local only."])
        document = documents.extract_document("/private/path/report.pdf", payload)

        self.assertEqual(document.filename, "report.pdf")
        self.assertEqual(document.page_count, 2)
        self.assertIn("First PDF page has readable text.", document.pages[0].text)
        self.assertIn("Second page says local only.", document.pages[1].text)

    def test_rejects_malformed_encrypted_and_textless_pdfs(self):
        with self.assertRaisesRegex(documents.DocumentError, "malformed|unsupported"):
            documents.extract_document("broken.pdf", b"%PDF-not a real file")

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("local-password")
        encrypted = io.BytesIO()
        writer.write(encrypted)
        with self.assertRaisesRegex(documents.DocumentError, "Encrypted"):
            documents.extract_document("locked.pdf", encrypted.getvalue())

        with self.assertRaisesRegex(documents.DocumentError, "No selectable text"):
            documents.extract_document("scan.pdf", make_pdf([""]))

    def test_rejects_unsupported_and_mismatched_content(self):
        with self.assertRaisesRegex(documents.DocumentError, "Only .txt and .pdf"):
            documents.extract_document("document.docx", b"data")
        with self.assertRaisesRegex(documents.DocumentError, "PDF signature"):
            documents.extract_document("fake.pdf", b"not a pdf")
        with self.assertRaisesRegex(documents.DocumentError, "UTF-8 text"):
            documents.extract_document("binary.txt", b"%PDF-1.4\x00")

    def test_rejects_empty_and_oversized_files(self):
        with self.assertRaisesRegex(documents.DocumentError, "empty"):
            documents.extract_document("empty.txt", b"")
        with self.assertRaisesRegex(documents.DocumentError, "5 MiB"):
            documents.extract_document("large.txt", b"a" * (documents.MAX_UPLOAD_BYTES + 1))

    def test_rejects_pdf_stream_over_decompression_limit(self):
        oversized_stream = make_pdf(
            ["repeated text " * (documents.MAX_PDF_STREAM_BYTES // 8)], compress=True
        )

        with self.assertRaisesRegex(documents.DocumentError, "decompression limits"):
            documents.extract_document("compressed.pdf", oversized_stream)

    def test_long_document_selection_is_relevant_and_bounded(self):
        pages = (
            documents.DocumentPage(1, "\n\n".join(f"General introduction paragraph {i}." for i in range(30))),
            documents.DocumentPage(2, "The zebra compensation schedule allows 18 months of coverage."),
        )
        document = documents.SelectedDocument(
            "policy.pdf", "PDF", pages, 2, sum(len(page.text) for page in pages)
        )

        selection = documents.select_document_context(
            document, "What does the zebra compensation schedule allow?", 500
        )

        self.assertEqual(selection.mode, "excerpt")
        self.assertLessEqual(len(selection.text), 500)
        self.assertIn("zebra compensation schedule", selection.text)
        self.assertNotIn("General introduction paragraph 0", selection.text)

    def test_document_prompt_discloses_truncation_and_untrusted_content(self):
        document = documents.SelectedDocument(
            "large.txt",
            "Text",
            (documents.DocumentPage(1, "Extracted passage."),),
            1,
            documents.MAX_EXTRACTED_CHARS,
            truncated=True,
        )
        message, selection = documents.document_context_message(document, "What is stated?")

        self.assertIn("untrusted quoted data", message["content"])
        self.assertIn("remainder of the document was not extracted or considered", message["content"])
        self.assertEqual(selection.mode, "excerpt_truncated")


class DocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "data" / "test.sqlite3"
        self.client = web_app.create_app(self.database_path).test_client()
        self.conversation_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def upload(self, conversation_id, filename, content):
        return self.client.post(
            "/api/documents",
            data={
                "conversation_id": conversation_id,
                "file": (io.BytesIO(content), filename),
            },
            content_type="multipart/form-data",
        )

    def test_upload_validation_and_size_limit(self):
        self.assertEqual(self.client.post("/api/documents", data={}).status_code, 400)
        self.assertEqual(
            self.upload(self.conversation_id, "unsupported.csv", b"a,b").status_code, 422
        )
        self.assertEqual(
            self.upload(self.conversation_id, "fake.pdf", b"plain text").status_code, 422
        )
        with patch.object(web_app, "MAX_UPLOAD_BYTES", 64):
            oversized = self.upload(self.conversation_id, "large.txt", b"a" * 65)
        self.assertEqual(oversized.status_code, 413)
        self.assertIn("5 MiB", oversized.get_json()["error"])
        oversized.close()

    def test_context_is_passed_to_shared_chat_and_clear_restores_normal_chat(self):
        uploaded = self.upload(self.conversation_id, "facts.txt", b"The device warranty lasts 18 months.")
        self.assertEqual(uploaded.status_code, 201)
        self.assertEqual(uploaded.get_json()["document"]["filename"], "facts.txt")

        with patch.object(
            AssistantRuntime,
            "run_turn",
            return_value=TurnResult("It lasts 18 months.", []),
        ) as respond:
            chat = self.client.post(
                "/api/chat", json={"conversation_id": self.conversation_id, "message": "How long is it?"}
            )
            self.assertEqual(chat.status_code, 200)
            self.assertIn(
                "The device warranty lasts 18 months.",
                respond.call_args.kwargs["additional_context"][0]["content"],
            )
            self.assertEqual(chat.get_json()["document_context"], "full")

            cleared = self.client.delete(f"/api/conversations/{self.conversation_id}/document")
            self.assertEqual(cleared.status_code, 204)
            self.client.post(
                "/api/chat", json={"conversation_id": self.conversation_id, "message": "Normal chat now."}
            )
            self.assertEqual(respond.call_args.kwargs["additional_context"], [])

    def test_document_context_does_not_leak_between_conversations(self):
        other_id = self.client.post("/api/conversations", json={}).get_json()["conversation_id"]
        self.upload(self.conversation_id, "private.txt", b"Secret document phrase: saffron river.")

        with patch.object(
            AssistantRuntime,
            "run_turn",
            return_value=TurnResult("Ordinary answer.", []),
        ) as respond:
            response = self.client.post(
                "/api/chat", json={"conversation_id": other_id, "message": "What is in the file?"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(respond.call_args.kwargs["additional_context"], [])
        self.assertNotIn("saffron river", response.get_data(as_text=True))

    def test_replacing_document_resets_history_and_is_not_persisted(self):
        self.upload(self.conversation_id, "first.txt", b"First document contents are private.")
        second_upload = self.upload(self.conversation_id, "second.txt", b"Second document contents are private.")
        self.assertTrue(second_upload.get_json()["conversation_reset"])

        connection = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?", ("table",))
            }
        finally:
            connection.close()
        self.assertNotIn("documents", tables)
        self.assertNotIn(b"Second document contents", self.database_path.read_bytes())

    def test_new_conversation_discards_previous_document(self):
        self.upload(self.conversation_id, "private.txt", b"Old document content.")
        new_id = self.client.post(
            "/api/conversations", json={"previous_id": self.conversation_id}
        ).get_json()["conversation_id"]

        with patch.object(
            AssistantRuntime,
            "run_turn",
            return_value=TurnResult("No document context.", []),
        ) as respond:
            response = self.client.post(
                "/api/chat", json={"conversation_id": new_id, "message": "Hello"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(respond.call_args.kwargs["additional_context"], [])


if __name__ == "__main__":
    unittest.main()