"""task1/state.py のテスト。

**この層の失敗は、静かではなく高くつく。**

前回どこまで読んだかを見失うと、次の実行は全履歴を読み直して巨大な要約を作り、
LINE へ1通送る。無料プランは月200通、Gemini は呼ぶたびに金額が動く。
だから「分からなくなったら止まる」を仕様にする。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state  # noqa: E402

CHANNEL = "C0BQFU7STLM"


class TempPath(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.path = Path(self._dir.name) / "state.json"

    def tearDown(self):
        self._dir.cleanup()


class Load(TempPath):
    def test_missing_file_is_the_first_run(self):
        """**ファイルが無いのは正常**。初回はここから始まる。"""
        loaded = state.load(self.path)

        self.assertIsNone(state.cursor_for(loaded, CHANNEL))

    def test_broken_json_is_an_error_not_a_first_run(self):
        """**これがこのモジュールの主題。**

        壊れた JSON を「読めなかった＝初回」と解釈すると、次の実行は
        チャンネルの全履歴を読み直す。要約は巨大になり、通数と課金を焼く。
        **分からなくなったことを、分からないと言う。**
        """
        self.path.write_text("{壊れている", encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(self.path)

    def test_wrong_top_level_shape_is_an_error(self):
        """JSON として読めても、形が違えば同じ危険がある。"""
        self.path.write_text("[1, 2, 3]", encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(self.path)

    def test_non_string_cursor_is_an_error(self):
        """``ts`` が数値で入っていたら止める。

        ``1503435956.000247`` を float として読むと倍精度で表しきれず、
        **別のメッセージを指す**（課題7の実測）。黙って文字列化すると、
        そのズレたまま位置が進む。
        """
        self.path.write_text(json.dumps({"cursors": {CHANNEL: 1503435956.000247}}), encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(self.path)

    def test_roundtrip(self):
        saved = state.advanced(state.empty(), CHANNEL, "1786915517.894839")
        state.save(self.path, saved)

        loaded = state.load(self.path)

        self.assertEqual(state.cursor_for(loaded, CHANNEL), "1786915517.894839")

    def test_other_channels_are_kept(self):
        """**チャンネルごとに持つ。**

        1本の値を使い回すと、見るチャンネルを変えたときに前の位置を使う。
        大量に読み直すか、何も読まないかのどちらかになる。
        """
        first = state.advanced(state.empty(), CHANNEL, "100")
        second = state.advanced(first, "C_OTHER", "200")

        self.assertEqual(state.cursor_for(second, CHANNEL), "100")
        self.assertEqual(state.cursor_for(second, "C_OTHER"), "200")


class Advanced(unittest.TestCase):
    def test_returns_a_new_object(self):
        """**元を書き換えない。**

        呼び出し側は「送信に成功したら進める」順で使う。元が変わる実装だと、
        送信に失敗した経路でも進んだ値が残り、取りこぼす。
        """
        before = state.advanced(state.empty(), CHANNEL, "100")

        after = state.advanced(before, CHANNEL, "200")

        self.assertEqual(state.cursor_for(before, CHANNEL), "100")
        self.assertEqual(state.cursor_for(after, CHANNEL), "200")

    def test_blank_ts_is_refused(self):
        """空で進めない。空を書くと次回は初回と同じ挙動になる。"""
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    state.advanced(state.empty(), CHANNEL, value)


class Save(TempPath):
    def test_written_as_utf8_json(self):
        state.save(self.path, state.advanced(state.empty(), CHANNEL, "100"))

        payload = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(payload["cursors"][CHANNEL], "100")

    def test_creates_parent_directories(self):
        nested = self.path.parent / "a" / "b" / "state.json"

        state.save(nested, state.advanced(state.empty(), CHANNEL, "100"))

        self.assertTrue(nested.is_file())

    def test_existing_file_survives_a_failed_write(self):
        """**書き換えは差し替えで行う。**

        書いている途中で落ちると、開いていたファイルは半端な JSON で残る。
        次回はそれを読んで ``StateError`` で止まる——安全ではあるが、
        **人が直すまで動かない**。一時ファイルに書いてから差し替えれば、
        落ちても元の内容が残る。
        """
        state.save(self.path, state.advanced(state.empty(), CHANNEL, "100"))
        original = self.path.read_text(encoding="utf-8")

        broken = state.State(cursors={CHANNEL: object()})  # JSON にできない値
        with self.assertRaises(Exception):
            state.save(self.path, broken)

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

    def test_write_goes_through_a_temporary_file(self):
        """**一時ファイル経由であること自体を固定する。**

        「失敗しても元が残る」テストだけでは足りなかった——``json.dumps`` を
        先に済ませているので、直接上書きに書き換えても**元は残る**。
        つまりミューテーションが素通りした（2026-08-29）。

        耐障害性は「落ちた瞬間に半端なファイルを作らないこと」なので、
        そこを直接見る。書き込み中に落とすのは再現しにくいため、
        差し替え（``os.replace``）が使われたことを観測する。
        """
        import state as state_module

        seen = []
        original = state_module.os.replace

        def spy(src, dst):
            seen.append((Path(src).name, Path(dst).name))
            return original(src, dst)

        state_module.os.replace = spy
        try:
            state.save(self.path, state.advanced(state.empty(), CHANNEL, "100"))
        finally:
            state_module.os.replace = original

        self.assertEqual(len(seen), 1)
        source, destination = seen[0]
        self.assertTrue(source.endswith(".tmp"))
        self.assertEqual(destination, self.path.name)

    def test_no_leftover_temp_files(self):
        state.save(self.path, state.advanced(state.empty(), CHANNEL, "100"))

        siblings = [p.name for p in self.path.parent.iterdir()]

        self.assertEqual(siblings, [self.path.name])


if __name__ == "__main__":
    unittest.main()
