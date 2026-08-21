import unittest

from main import build_weather_payload, normalize_weather_location, select_weather_location, weather_code_details


class WeatherTests(unittest.TestCase):
    def test_weather_location_normalizes_whitespace_and_umlauts(self):
        self.assertEqual(normalize_weather_location("  München   Zentrum  "), "München Zentrum")

    def test_weather_location_rejects_invalid_lengths(self):
        with self.assertRaises(ValueError):
            normalize_weather_location("A")
        with self.assertRaises(ValueError):
            normalize_weather_location("x" * 121)

    def test_weather_codes_have_readable_german_labels(self):
        self.assertEqual(weather_code_details(0), ("Klar", "☀"))
        self.assertEqual(weather_code_details(65), ("Regen", "🌧"))
        self.assertEqual(weather_code_details(95), ("Gewitter", "⛈"))

    def test_weather_payload_uses_current_and_daily_values(self):
        payload = build_weather_payload(
            "Bechhofen",
            {"results": [{"name": "Bechhofen", "country_code": "DE", "admin1": "Bayern"}]},
            {
                "current": {"temperature_2m": 18.4, "weather_code": 2},
                "daily": {
                    "weather_code": [3],
                    "temperature_2m_min": [10.2],
                    "temperature_2m_max": [21.8],
                    "precipitation_probability_max": [35],
                },
            },
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["location"], "Bechhofen, Bayern")
        self.assertEqual(payload["condition"], "Leicht bewölkt")
        self.assertEqual(payload["temperature"], 18.4)
        self.assertEqual(payload["temperature_min"], 10.2)
        self.assertEqual(payload["temperature_max"], 21.8)
        self.assertEqual(payload["precipitation_probability"], 35)

    def test_weather_payload_rejects_unknown_location(self):
        with self.assertRaises(ValueError):
            build_weather_payload("Unbekannt", {"results": []}, {})

    def test_region_qualifier_selects_matching_location(self):
        results = [
            {"name": "Neustadt", "admin1": "Hessen"},
            {"name": "Neustadt", "admin1": "Bayern"},
        ]
        selected = select_weather_location("Neustadt, Bayern", results)
        self.assertEqual(selected["admin1"], "Bayern")


if __name__ == "__main__":
    unittest.main()
