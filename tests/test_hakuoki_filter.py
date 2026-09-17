"""Regression fixtures for the per-game read-indicator cleanup."""
from pathlib import Path
import re
import unittest

import yaml


class HakuokiFilter(unittest.TestCase):
    def test_standalone_markers_only(self):
        path = Path(__file__).resolve().parents[1] / 'profiles.yml'
        if not path.exists():
            self.skipTest('Example profiles excluded from container images')
        data = yaml.safe_load(path.read_text())
        pattern = data['game_overrides']['Hakuoki: Chronicles of Wind and Blossom']['ocr_strip']
        clean = lambda text: re.sub(pattern, '', text, flags=re.UNICODE).strip()
        fixtures = [
            ('既\n読\n雪村千鶴\n「そうですよね・・・」', '雪村千鶴\n「そうですよね・・・」'),
            ('台詞です。\n既\n読', '台詞です。'),
            ('既読\n台詞です。', '台詞です。'),
            ('読◆きどく\n台詞です。', '台詞です。'),
            ('　既 読　\r\n台詞です。', '台詞です。'),
            ('既に読んだ本を読む。', '既に読んだ本を読む。'),
            ('「既読になっています」', '「既読になっています」'),
            ('台詞の末尾に読', '台詞の末尾に読'),
            ('一行目\n二行目', '一行目\n二行目'),
        ]
        for source, expected in fixtures:
            with self.subTest(source=source):
                self.assertEqual(clean(source), expected)


if __name__ == '__main__':
    unittest.main()
