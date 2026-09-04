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


class ThreadWatch(TempPath):
    """スレッドの「どの親を、どこまで読んだか」。

    チャンネル本文の位置（``cursors``）では代用できない。返信の ts は親より
    後なので、返信で本文の位置を進めると**親より後のチャンネル投稿を飛ばす**。
    だから位置を親ごとに分けて持つ。

    見張る親は**増え続ける**ので窓で切る。**切ったことは静かにしない**——
    落ちた親の返信は二度と読まれないので、呼ぶ側が画面に出せる形で返す。
    """

    def test_old_file_without_threads_still_loads(self):
        """``threads`` を持たない状態ファイルを読める。

        この欄は後から足した。**古いファイルで止まると、位置を見失った扱いに
        なって全履歴を読み直す**——月200通の枠と Gemini の課金が動く。
        """
        path = self.path
        path.write_text('{"cursors": {"C1": "100"}}', encoding="utf-8")

        current = state.load(path)

        self.assertEqual(current.cursors, {"C1": "100"})
        self.assertEqual(state.watch_for(current, "C1"), {})

    def test_threads_must_be_a_mapping(self):
        path = self.path
        path.write_text('{"cursors": {}, "threads": []}', encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(path)

    def test_threads_per_channel_must_be_a_mapping(self):
        path = self.path
        path.write_text('{"cursors": {}, "threads": {"C1": "x"}}', encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(path)

    def test_reply_position_must_be_a_string(self):
        """数値で入っていたら黙って文字列化しない（``cursors`` と同じ判断）。"""
        path = self.path
        path.write_text('{"cursors": {}, "threads": {"C1": {"1.1": 2}}}', encoding="utf-8")

        with self.assertRaises(state.StateError):
            state.load(path)

    def test_watching_adds_new_parents(self):
        current, dropped = state.watching(state.empty(), "C1", ["10.1", "20.2"])

        self.assertEqual(state.watch_for(current, "C1"), {"10.1": "", "20.2": ""})
        self.assertEqual(dropped, ())

    def test_watching_keeps_what_was_already_read(self):
        """既に読んだ位置を**上書きしない**。上書きすると返信を読み直して重複する。"""
        first = state.replies_advanced(
            state.watching(state.empty(), "C1", ["10.1"])[0], "C1", {"10.1": "11.5"}
        )

        second, _ = state.watching(first, "C1", ["10.1", "20.2"])

        self.assertEqual(state.watch_for(second, "C1"), {"10.1": "11.5", "20.2": ""})

    def test_window_drops_the_oldest_and_says_which(self):
        """窓を超えたら古い親から落ちる。**落ちた ts を返す。**"""
        parents = [f"{i}.0" for i in range(1, state.WATCH_LIMIT + 3)]

        current, dropped = state.watching(state.empty(), "C1", parents)

        self.assertEqual(len(state.watch_for(current, "C1")), state.WATCH_LIMIT)
        self.assertEqual(dropped, ("1.0", "2.0"))

    def test_window_keeps_the_newest(self):
        parents = [f"{i}.0" for i in range(1, state.WATCH_LIMIT + 3)]

        current, _ = state.watching(state.empty(), "C1", parents)

        self.assertIn(f"{state.WATCH_LIMIT + 2}.0", state.watch_for(current, "C1"))

    def test_channels_do_not_share_a_window(self):
        current, _ = state.watching(state.empty(), "C1", ["10.1"])
        current, _ = state.watching(current, "C2", ["20.2"])

        self.assertEqual(state.watch_for(current, "C1"), {"10.1": ""})
        self.assertEqual(state.watch_for(current, "C2"), {"20.2": ""})

    def test_replies_advanced_does_not_mutate_the_original(self):
        """**元を書き換えない。** 送信に失敗した経路で進んだ値が残ると取りこぼす。"""
        before, _ = state.watching(state.empty(), "C1", ["10.1"])

        state.replies_advanced(before, "C1", {"10.1": "11.5"})

        self.assertEqual(state.watch_for(before, "C1"), {"10.1": ""})

    def test_replies_advanced_ignores_parents_we_are_not_watching(self):
        """見張っていない親を勝手に足さない。窓の意味が消える。"""
        before, _ = state.watching(state.empty(), "C1", ["10.1"])

        after = state.replies_advanced(before, "C1", {"99.9": "100.0"})

        self.assertEqual(state.watch_for(after, "C1"), {"10.1": ""})

    def test_round_trip_through_the_file(self):
        path = self.path
        current, _ = state.watching(state.empty(), "C1", ["10.1"])
        current = state.replies_advanced(current, "C1", {"10.1": "11.5"})

        state.save(path, current)

        self.assertEqual(state.watch_for(state.load(path), "C1"), {"10.1": "11.5"})
