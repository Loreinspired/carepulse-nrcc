from django.test import SimpleTestCase

from outreach.utils import (
    PhoneNormalizationError,
    build_wa_me_url,
    e164_to_wa_id,
    generate_qr_svg,
    normalize_nigeria_phone,
)


class PhoneNormalizationTests(SimpleTestCase):
    def test_standard_nigerian_formats(self):
        expected = "+2348031234567"
        variants = [
            "08031234567",
            "0803 123 4567",
            "0803-123-4567",
            "8031234567",
            "2348031234567",
            "+2348031234567",
            "+234 803 123 4567",
            "23408031234567",
            "+23408031234567",
            "002348031234567",
        ]
        for raw in variants:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_nigeria_phone(raw), expected)

    def test_other_valid_nigerian_prefixes(self):
        self.assertEqual(normalize_nigeria_phone("07011223344"), "+2347011223344")
        self.assertEqual(normalize_nigeria_phone("08149876543"), "+2348149876543")
        self.assertEqual(normalize_nigeria_phone("09051122334"), "+2349051122334")
        self.assertEqual(normalize_nigeria_phone("09161234567"), "+2349161234567")

    def test_explicit_international_e164_preserved(self):
        self.assertEqual(normalize_nigeria_phone("+447911123456"), "+447911123456")
        self.assertEqual(normalize_nigeria_phone("+1 202-555-0142"), "+12025550142")

    def test_invalid_numbers_raise(self):
        invalid_inputs = [
            "",
            "   ",
            None,
            "0803abc4567",
            "0803123",
            "06012345678",
            "+2341234567890",
            "12345",
        ]
        for bad in invalid_inputs:
            with self.subTest(bad=bad):
                with self.assertRaises(PhoneNormalizationError):
                    normalize_nigeria_phone(bad)

    def test_wa_id_and_wame_url(self):
        self.assertEqual(e164_to_wa_id("+2348031234567"), "2348031234567")
        url = build_wa_me_url("+2348031234567", "Peace Tunde!")
        self.assertEqual(url, "https://wa.me/2348031234567?text=Peace%20Tunde%21")

    def test_generate_qr_svg(self):
        svg = generate_qr_svg("https://wa.me/2348000000000?text=Hi")
        self.assertIn("<svg", svg)
        self.assertIn("</svg>", svg)
