from __future__ import annotations

import unittest

from core.qr_login import QRCodePayload, QRLoginEvent, QRStatusPayload


class QRLoginTests(unittest.TestCase):
    def test_qr_code_payload_from_dict_normalizes_fields(self) -> None:
        payload = QRCodePayload.from_dict(
            {
                "qrcode": " code-123 ",
                "qrcode_img_content": " image-abc ",
            }
        )

        self.assertEqual("code-123", payload.code)
        self.assertEqual("image-abc", payload.image_content)

    def test_qr_status_payload_from_dict_normalizes_fields(self) -> None:
        payload = QRStatusPayload.from_dict(
            {
                "status": " confirmed ",
                "redirect_host": " login.example.com ",
                "ticket": "abc",
            }
        )

        self.assertEqual("confirmed", payload.status)
        self.assertEqual("login.example.com", payload.redirect_host)
        self.assertEqual("abc", payload.raw_payload["ticket"])

    def test_qr_login_event_accessors_normalize_payload(self) -> None:
        event = QRLoginEvent(
            type="qr_code",
            payload={
                "base_url": " https://example.com ",
                "qrcode_img_content": " abc123 ",
                "message": " done ",
            },
        )

        self.assertEqual("https://example.com", event.base_url)
        self.assertEqual("abc123", event.image_content)
        self.assertEqual("done", event.message)


if __name__ == "__main__":
    unittest.main()
