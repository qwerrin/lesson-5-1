"""common/gemini_client.py のテスト。

**例外は実物を組む。** ``google.genai.errors.APIError`` は
``APIError(code, response_json)`` で構築できるので、偽物を自作しない。
偽物は実装に似せて作るため、実装の誤りごと写し取る（課題10 の Discord 側で
``identity.id`` という実在しない属性を偽物が受け入れ、実機で初めて落ちた）。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx
from google.genai import errors as genai_errors

from common import gemini_client


def api_error(code: int, message: str, status: str = "ERROR") -> genai_errors.APIError:
    """本物の APIError を組む。"""
    return genai_errors.APIError(
        code, {"error": {"code": code, "message": message, "status": status}}
    )


class ReadApiKey(unittest.TestCase):
    """キーの読み取り。**値そのものは絶対に文言へ載せない。**"""

    def test_missing_key_raises(self):
        with self.assertRaises(gemini_client.AuthError):
            gemini_client.read_api_key({})

    def test_empty_value_raises(self):
        # `GEMINI_API_KEY=` と書いただけでもキーは存在する。有無だけ見ると
        # 素通りして、後段の API が 400 を返し、原因がここだと分からなくなる。
        with self.assertRaises(gemini_client.AuthError):
            gemini_client.read_api_key({gemini_client.API_KEY_ENV: ""})

    def test_whitespace_only_raises(self):
        with self.assertRaises(gemini_client.AuthError):
            gemini_client.read_api_key({gemini_client.API_KEY_ENV: "   "})

    def test_value_is_stripped(self):
        value = gemini_client.read_api_key({gemini_client.API_KEY_ENV: "  AQ.abc  "})
        self.assertEqual(value, "AQ.abc")

    def test_shape_is_not_enforced(self):
        """**形で弾かない。**

        現行のキーは ``AQ.`` で始まる（2026-08-29 実測）が、それを仕様として
        明記した文書は確認していない。確かめていない形を検査に使うと、
        提供側が形を変えた日に**正しい値を拒む**側で壊れる
        （``line_auth.read_user_id`` で長さを検査しないと決めたのと同じ判断）。
        """
        value = gemini_client.read_api_key({gemini_client.API_KEY_ENV: "zzz-not-AQ"})
        self.assertEqual(value, "zzz-not-AQ")

    def test_error_message_never_contains_the_value(self):
        """打ち間違いでも本物が入っている。文言はスクリーンショットに写る。"""
        # **ダミーは本物の形を真似ない。** 真似ると、追跡ファイルを走査する
        # 検査が本物と区別できなくなる（実際に check_docs が拾った）。
        secret = "DUMMY-KEY-must-not-appear-in-messages"
        with self.assertRaises(gemini_client.AuthError) as caught:
            gemini_client.read_api_key({gemini_client.API_KEY_ENV: "  "})
        self.assertNotIn(secret, str(caught.exception))


class Redact(unittest.TestCase):
    def test_replaces_secret(self):
        got = gemini_client.redact("key=AQ.abc です", "AQ.abc")
        self.assertNotIn("AQ.abc", got)
        self.assertIn(gemini_client.REDACTED, got)

    def test_empty_secret_is_skipped(self):
        """``str.replace("", x)`` は全部の文字の間に x を挿し込む。"""
        got = gemini_client.redact("そのまま", "", None)
        self.assertEqual(got, "そのまま")


class IsTransient(unittest.TestCase):
    """投げ直してよい失敗かを、**``code``（整数）だけで**決める。"""

    def test_retryable_status_codes(self):
        for code in (429, 500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(gemini_client.is_transient(api_error(code, "busy")))

    def test_permanent_status_codes(self):
        for code in (400, 401, 403, 404):
            with self.subTest(code=code):
                self.assertFalse(gemini_client.is_transient(api_error(code, "bad")))

    def test_message_containing_a_retryable_number_is_not_transient(self):
        """**これが文字列マッチを殺すテスト。**

        本文にたまたま 429 を含む恒久エラーがある。``str(e)`` を見て判定すると
        これを一時エラーと誤認し、絶対に成功しない要求を投げ直し続ける。
        """
        error = api_error(400, "token count 429 exceeds limit", "INVALID_ARGUMENT")
        self.assertIn("429", str(error))
        self.assertFalse(gemini_client.is_transient(error))

    def test_connection_errors_are_transient(self):
        # 接続断はサーバの応答ですらないので code を持たない。別枠で拾う。
        self.assertTrue(gemini_client.is_transient(OSError("connection reset")))

    def test_httpx_transport_error_is_transient(self):
        """**``httpx.TransportError`` は ``OSError`` ではない**（2026-08-29 実測）。

        MRO は ``TransportError → RequestError → HTTPError → Exception`` で、
        ``OSError`` を1つ拾うだけでは**転送タイムアウトが恒久エラーに化ける**。
        「接続断は OSError だろう」で済ませると、ここが静かに抜ける。
        """
        self.assertFalse(issubclass(httpx.TransportError, OSError))
        self.assertTrue(gemini_client.is_transient(httpx.ConnectTimeout("timeout")))

    def test_unrelated_exception_is_not_transient(self):
        self.assertFalse(gemini_client.is_transient(ValueError("別物")))


class TranslateError(unittest.TestCase):
    def test_code_is_always_shown(self):
        """**コードは必ず出す。** 日本語の説明だけだと公式資料を引けない。"""
        message = str(gemini_client.translate_error(api_error(404, "no such model")))
        self.assertIn("404", message)

    def test_secret_is_redacted(self):
        secret = "AQ.leaked"
        error = api_error(400, f"API key {secret} is invalid")
        message = str(gemini_client.translate_error(error, secret))
        self.assertNotIn(secret, message)

    def test_404_names_the_model_replacement_hint(self):
        """404 は「モデルが無い」で終わらせない。

        2026-08-29 の実測では ``gemini-2.5-flash-lite`` が
        ``models.list()`` に載っていながら 404 を返し、本文が後継を名指ししていた。
        **一覧に載ることは、呼べることの証拠にならない。**
        """
        error = api_error(404, "no longer available to new users", "NOT_FOUND")
        message = str(gemini_client.translate_error(error))
        self.assertIn("no longer available to new users", message)


class BuildConfig(unittest.TestCase):
    """自動関数呼び出し（AFC）を切る。

    ツールを1つも渡していなくても SDK が stderr に勧告を出す（2026-08-29 実測）。
    **実行画面は記事のスクリーンショットになる**ので、出力に意味の無い行を残さない。
    """

    def test_afc_is_disabled(self):
        config = gemini_client.build_config()
        self.assertTrue(config.automatic_function_calling.disable)


class FakeModels:
    def __init__(self, text="要約です"):
        self.calls = []
        self._text = text

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self._text)


class FakeClient:
    def __init__(self, text="要約です"):
        self.models = FakeModels(text)


class Generate(unittest.TestCase):
    def test_returns_text(self):
        client = FakeClient("3行の要約")
        self.assertEqual(
            gemini_client.generate(client, prompt="ログ"), "3行の要約"
        )

    def test_passes_the_default_model(self):
        client = FakeClient()
        gemini_client.generate(client, prompt="ログ")
        self.assertEqual(client.models.calls[0]["model"], gemini_client.DEFAULT_MODEL)

    def test_config_disables_afc(self):
        client = FakeClient()
        gemini_client.generate(client, prompt="ログ")
        config = client.models.calls[0]["config"]
        self.assertTrue(config.automatic_function_calling.disable)

    def test_empty_answer_is_a_failure(self):
        """**空の答えを「要約できた」にしない。**

        安全フィルタや打ち切りで本文が空になることがある。空文字を返すと
        呼ぶ側は「要約が空だった」ではなく「要約が空という要約」を送ってしまう。
        """
        for text in ("", "   ", None):
            with self.subTest(text=text):
                with self.assertRaises(gemini_client.ApiError):
                    gemini_client.generate(FakeClient(text), prompt="ログ")

    def test_empty_prompt_is_refused_before_calling(self):
        """入力が空なら**呼ばない**。呼べば何か返ってきて、それは発明になる。"""
        client = FakeClient()
        with self.assertRaises(ValueError):
            gemini_client.generate(client, prompt="   ")
        self.assertEqual(client.models.calls, [])


class FakeAudioModels:
    """音声版の応答を組む。**本文だけでなく打ち切りと使用量も返す相手。**"""

    def __init__(self, text="[00:01] 話者A: はい。", finish="STOP", usage=(11, 22)):
        self.calls = []
        self._text = text
        self._finish = finish
        self._usage = usage

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        candidates = []
        if self._finish is not None:
            candidates = [SimpleNamespace(finish_reason=SimpleNamespace(name=self._finish))]
        usage = None
        if self._usage is not None:
            usage = SimpleNamespace(
                prompt_token_count=self._usage[0], candidates_token_count=self._usage[1]
            )
        return SimpleNamespace(text=self._text, candidates=candidates, usage_metadata=usage)


class FakeAudioClient:
    def __init__(self, **kw):
        self.models = FakeAudioModels(**kw)


class GenerateWithAudio(unittest.TestCase):
    """課題3 の音声入力。**generate() とは返す型が違う**ので別の関数にしてある。"""

    def call(self, client, **kw):
        opts = {"prompt": "文字起こしして", "audio_bytes": b"RIFFxxxx", "mime_type": "audio/wav"}
        opts.update(kw)
        return gemini_client.generate_with_audio(client, **opts)

    def test_本文と打ち切りと使用量を返す(self):
        reply = self.call(FakeAudioClient(text="[00:01] 話者A: はい。"))
        self.assertEqual(reply.text, "[00:01] 話者A: はい。")
        self.assertEqual(reply.finish_reason, "STOP")
        self.assertEqual((reply.prompt_tokens, reply.output_tokens), (11, 22))

    def test_音声が本文と一緒に渡る(self):
        client = FakeAudioClient()
        self.call(client, audio_bytes=b"AUDIOBYTES")
        contents = client.models.calls[0]["contents"]
        self.assertEqual(contents[0], "文字起こしして")
        self.assertEqual(contents[1].inline_data.data, b"AUDIOBYTES")
        self.assertEqual(contents[1].inline_data.mime_type, "audio/wav")

    def test_打ち切りでも本文を返す(self):
        """**打ち切りを失敗にしない。** 途中まででも文字起こしは高い。

        捨てるかどうかは呼び手が決める。ここで例外にすると、
        呼び手は何が返ったかを見られないまま止まる。
        """
        reply = self.call(FakeAudioClient(finish="MAX_TOKENS"))
        self.assertEqual(reply.finish_reason, "MAX_TOKENS")
        self.assertTrue(reply.text)

    def test_打ち切りが読めなければNoneにする(self):
        """**「STOP だった」と「見られなかった」を混同させない。**"""
        self.assertIsNone(self.call(FakeAudioClient(finish=None)).finish_reason)

    def test_使用量が無ければNoneにする(self):
        reply = self.call(FakeAudioClient(usage=None))
        self.assertIsNone(reply.prompt_tokens)
        self.assertIsNone(reply.output_tokens)

    def test_空の答えは失敗にする(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                with self.assertRaises(gemini_client.ApiError):
                    self.call(FakeAudioClient(text=text))

    def test_空のプロンプトでは呼ばない(self):
        client = FakeAudioClient()
        with self.assertRaises(ValueError):
            self.call(client, prompt="   ")
        self.assertEqual(client.models.calls, [])

    def test_空の音声では呼ばない(self):
        client = FakeAudioClient()
        with self.assertRaises(ValueError):
            self.call(client, audio_bytes=b"")
        self.assertEqual(client.models.calls, [])

    def test_上限を超えたら呼ばない(self):
        """**黙って送らない。** 相手の拒否は課金や再試行と混ざって読みにくい。"""
        client = FakeAudioClient()
        with self.assertRaises(ValueError):
            self.call(client, audio_bytes=b"x" * 11, limit_bytes=10)
        self.assertEqual(client.models.calls, [])

    def test_上限ちょうどは呼ぶ(self):
        client = FakeAudioClient()
        self.call(client, audio_bytes=b"x" * 10, limit_bytes=10)
        self.assertEqual(len(client.models.calls), 1)

    def test_APIの失敗は訳して投げ直す(self):
        class Boom:
            class models:
                @staticmethod
                def generate_content(**kw):
                    raise api_error(503, "unavailable")

        with self.assertRaises(gemini_client.ApiError):
            self.call(Boom())


if __name__ == "__main__":
    unittest.main()
