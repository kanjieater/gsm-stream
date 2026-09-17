"""Regression fixtures for the Hiiro read/skip indicator cleanup."""
from pathlib import Path
import re
import unittest

import yaml


class HiiroFilter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'profiles.yml'
        if not path.exists():
            raise unittest.SkipTest('Example profiles excluded from container images')
        data = yaml.safe_load(path.read_text())
        cls.pattern = data['game_overrides']['Hiiro no Kakera Tamayori-hime Kitan: Omoi Iro no Kioku']['ocr_strip']

    def clean(self, text):
        return re.sub(self.pattern, '', text, flags=re.UNICODE).strip()

    def test_trailing_indicator_clusters(self):
        fixtures = [
            ('部屋中を調べたけど、おーちゃんはいなかった。既強白',
             '部屋中を調べたけど、おーちゃんはいなかった。'),
            ('「賛成です」既強\n白', '「賛成です」'),
            ('フィオナ先生は小さな口に笑みを浮かべる。既\n【',
             'フィオナ先生は小さな口に笑みを浮かべる。'),
            ('いつものように登校して、いつものように授業を\n受けて、いつものように昼休みになった。既強白',
             'いつものように登校して、いつものように授業を\n受けて、いつものように昼休みになった。'),
        ]
        for source, expected in fixtures:
            with self.subTest(source=source):
                self.assertEqual(self.clean(source), expected)

    def test_standalone_indicator_lines(self):
        for source, expected in [
            ('既\n祐一\n「いや、おまえのカンはよく当たると、\n俺のカンが告ている」',
             '祐一\n「いや、おまえのカンはよく当たると、\n俺のカンが告ている」'),
            ('自\nそして、全ては静止する。', 'そして、全ては静止する。'),
            ('既\n強\n白', ''),
            ('既読早送り\n台詞です。', '台詞です。'),
        ]:
            with self.subTest(source=source):
                self.assertEqual(self.clean(source), expected)

    def test_legitimate_text_preserved(self):
        for text in ['既に読んだ。', '白い雪。', '強大な、力。', '自分が行く',
                     '「白」', '既私はかえって、申し訳なくなった。',
                     '次の瞬間、彼は動いた。', '一行目\n二行目']:
            with self.subTest(text=text):
                self.assertEqual(self.clean(text), text)


if __name__ == '__main__':
    unittest.main()
