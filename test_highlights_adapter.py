
import unittest

from highlights_adapter import (
    normalize_collections,
    normalize_items,
)


class TestHighlightsAdapter(unittest.TestCase):

    def test_collections(self):
        dados = {
            "username": "perfilteste",
            "highlights": [
                {
                    "id": "123456",
                    "title": "Viagens",
                    "cover": "https://exemplo.com/capa.jpg",
                    "count": 2,
                }
            ],
        }

        resultado = normalize_collections(dados)

        self.assertEqual(
            resultado["username"], "perfilteste"
        )
        self.assertEqual(
            len(resultado["collections"]), 1
        )
        self.assertEqual(
            resultado["collections"][0]["id"], "123456"
        )
        self.assertEqual(
            resultado["collections"][0]["title"], "Viagens"
        )
        self.assertEqual(
            resultado["collections"][0]["total_stories"], 2
        )

    def test_video(self):
        dados = {
            "highlightId": "123456",
            "items": [
                {
                    "id": "789",
                    "videoUrl": "https://exemplo.com/video.mp4",
                    "cover": "https://exemplo.com/capa.jpg",
                    "images": [],
                }
            ],
            "hasMore": False,
        }

        resultado = normalize_items(dados)

        self.assertEqual(
            len(resultado["items"]), 1
        )
        self.assertEqual(
            resultado["items"][0]["type"], "video"
        )
        self.assertIsNone(resultado["next_cursor"])

    def test_photos_and_pagination(self):
        dados = {
            "highlightId": "123456",
            "items": [
                {
                    "id": "790",
                    "images": [
                        "https://exemplo.com/foto1.jpg",
                        "https://exemplo.com/foto2.jpg",
                    ],
                }
            ],
            "hasMore": True,
            "nextCursor": "10",
        }

        resultado = normalize_items(dados)

        self.assertEqual(
            resultado["items"][0]["type"], "image"
        )
        self.assertEqual(
            len(resultado["items"][0]["images"]), 2
        )
        self.assertEqual(
            resultado["next_cursor"], "10"
        )


if __name__ == "__main__":
    unittest.main()
