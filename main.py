"""英単語をGeminiで調べて、Notionの単語帳データベースに自動保存するCLI。

使い方:
    python main.py              # 対話モード（単語> に入力、空Enterで終了）
    python main.py run apple    # 引数で複数の単語をまとめて登録
"""

import os
import sys
import time
from pathlib import Path
from typing import Literal

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).parent / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_FALLBACK_MODELS = os.getenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Notionのプロパティ名（データベース側と完全に一致させる）
PROP_WORD = "単語"
PROP_MEANING = "意味"
PROP_POS = "品詞"
PROP_EXAMPLE = "例文"
PROP_EXAMPLE_JA = "例文訳"

PartOfSpeech = Literal[
    "名詞", "動詞", "形容詞", "副詞", "前置詞", "接続詞",
    "代名詞", "助動詞", "間投詞", "熟語", "その他",
]


class WordInfo(BaseModel):
    """Geminiに返させるJSONの形。response_schemaとして渡す。"""

    status: Literal["ok", "typo", "not_english"] = Field(
        description=(
            "ok: 入力が正しい英単語・熟語。"
            "typo: つづりミスと思われ、意図した英単語が推測できる。"
            "not_english: 英単語ではなく、意図した単語も推測できない"
        )
    )
    lemma: str = Field(
        description="辞書に載る原形（例: ran→run, mice→mouse）。typoの場合は正しいつづりの単語の原形"
    )
    meaning: str = Field(description="主要な日本語の意味。複数あれば読点で区切る")
    part_of_speech: PartOfSpeech = Field(description="最も一般的な品詞")
    example: str = Field(description="その単語を使った自然で短い英語の例文")
    example_ja: str = Field(description="例文の日本語訳")


def check_env() -> None:
    missing = [
        name
        for name, value in [
            ("GEMINI_API_KEY", GEMINI_API_KEY),
            ("NOTION_TOKEN", NOTION_TOKEN),
            ("NOTION_DATABASE_ID", NOTION_DATABASE_ID),
        ]
        if not value
    ]
    if missing:
        sys.exit(f".env に次の値が設定されていません: {', '.join(missing)}")


# ---------- Gemini ----------

gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
ATTEMPTS_PER_MODEL = 2
RETRYABLE_CODES = {500, 503}  # 混雑などの一時的なエラー → 少し待って再試行
SKIP_MODEL_CODES = {404, 429}  # 提供終了・無料枠の上限 → 再試行しても無駄なので次のモデルへ

# メインのモデルが混雑していたら、予備のモデルに順番に切り替える
MODELS = list(dict.fromkeys(
    [GEMINI_MODEL]
    + [m.strip() for m in GEMINI_FALLBACK_MODELS.split(",") if m.strip()]
))
_last_ok_model = None  # 直前に成功したモデルを次回最初に試す
_unavailable_models = set()  # 上限到達などで、このセッション中は使えないモデル


def generate_with_fallback(prompt: str, config: types.GenerateContentConfig):
    global _last_ok_model
    order = MODELS if _last_ok_model is None else [_last_ok_model] + [m for m in MODELS if m != _last_ok_model]
    order = [m for m in order if m not in _unavailable_models]
    if not order:
        raise RuntimeError("使えるモデルがありません。無料枠の上限に達した可能性があります。時間をおいて再実行してください。")
    last_error = None

    for model in order:
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            try:
                response = gemini.models.generate_content(model=model, contents=prompt, config=config)
                if model != GEMINI_MODEL and model != _last_ok_model:
                    print(f"  （予備モデル {model} を使用しました）")
                _last_ok_model = model
                return response
            except errors.APIError as e:
                last_error = e
                if e.code in SKIP_MODEL_CODES:
                    _unavailable_models.add(model)
                    break
                if e.code not in RETRYABLE_CODES:
                    raise
                if attempt < ATTEMPTS_PER_MODEL:
                    time.sleep(2 * attempt)
        reason = {429: "無料枠の上限に到達", 404: "提供終了"}.get(last_error.code, "混雑中")
        print(f"  … {model} は{reason}（{last_error.code}）のため、次のモデルを試します")

    raise last_error


def lookup_word(word: str) -> WordInfo:
    prompt = (
        "あなたは英和辞書です。次の英単語について、日本人の英語学習者向けに情報を返してください。\n"
        "活用形・複数形などが入力された場合は、lemmaに原形を入れ、原形の情報を返してください。\n"
        "つづりミスと思われる場合は、statusをtypoにし、最も可能性の高い正しい単語の情報を返してください。\n"
        "英単語でない場合は、statusをnot_englishにし、ほかの項目は空文字（品詞はその他）にしてください。\n"
        f"単語: {word}"
    )
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=WordInfo,
        temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = generate_with_fallback(prompt, config)
    if response.parsed is None:
        # parsedが取れない場合はテキストから検証し直す
        return WordInfo.model_validate_json(response.text)
    return response.parsed


# ---------- Notion ----------

def notion_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method: str, path: str, payload: dict) -> dict:
    res = requests.request(
        method, f"{NOTION_API}{path}", headers=notion_headers(), json=payload, timeout=30
    )
    if not res.ok:
        detail = res.json().get("message", res.text) if res.content else res.reason
        hint = ""
        if res.status_code == 404:
            hint = "\n  → データベースIDが正しいか、「接続」にインテグレーションを追加したか確認してください。"
        elif res.status_code == 401:
            hint = "\n  → NOTION_TOKEN が正しいか確認してください。"
        elif res.status_code == 400:
            hint = "\n  → プロパティ名・種類がデータベースと一致しているか確認してください。"
        raise RuntimeError(f"Notion API エラー ({res.status_code}): {detail}{hint}")
    return res.json()


def exists_in_notion(lemma: str) -> bool:
    data = notion_request(
        "POST",
        f"/databases/{NOTION_DATABASE_ID}/query",
        {"filter": {"property": PROP_WORD, "title": {"equals": lemma}}, "page_size": 1},
    )
    return len(data.get("results", [])) > 0


def rich_text(text: str) -> list:
    return [{"type": "text", "text": {"content": text[:2000]}}]


def save_to_notion(info: WordInfo) -> str:
    page = notion_request(
        "POST",
        "/pages",
        {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": {
                PROP_WORD: {"title": rich_text(info.lemma)},
                PROP_MEANING: {"rich_text": rich_text(info.meaning)},
                PROP_POS: {"select": {"name": info.part_of_speech}},
                PROP_EXAMPLE: {"rich_text": rich_text(info.example)},
                PROP_EXAMPLE_JA: {"rich_text": rich_text(info.example_ja)},
            },
        },
    )
    return page.get("url", "")


# ---------- メイン処理 ----------

def confirm(message: str) -> bool:
    try:
        answer = input(message).strip().lstrip("﻿").lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes", "はい")


def process(word: str) -> None:
    word = word.strip()
    if not word:
        return

    try:
        info = lookup_word(word)
    except Exception as e:
        print(f"  ✗ Geminiでの検索に失敗しました: {e}")
        return

    info.lemma = info.lemma.strip()

    if info.status == "not_english" or not info.lemma:
        print(f"  ✗ 「{word}」は英単語として認識されませんでした。")
        return

    if info.status == "typo":
        # つづりミスは勝手に保存せず、候補を見せて確認する
        if not confirm(f"  ？ 「{word}」はつづりミスかもしれません。もしかして「{info.lemma}」ですか？ [Y/n] "):
            print("  → 保存しませんでした。")
            return

    label = info.lemma if info.lemma.lower() == word.lower() else f"{info.lemma}（入力: {word}）"
    print(f"  {label} [{info.part_of_speech}]")
    print(f"  意味  : {info.meaning}")
    print(f"  例文  : {info.example}")
    print(f"  例文訳: {info.example_ja}")

    try:
        if exists_in_notion(info.lemma):
            print("  → 既に登録済みのため保存をスキップしました。")
            return
        url = save_to_notion(info)
        print(f"  ✓ Notionに保存しました {url}")
    except Exception as e:
        print(f"  ✗ {e}")


def main() -> None:
    check_env()

    words = sys.argv[1:]
    if words:
        for w in words:
            print(f"\n単語> {w}")
            process(w)
        return

    print(f"英単語を入力してください（空Enter / Ctrl+C で終了）  model={GEMINI_MODEL}")
    try:
        while True:
            word = input("\n単語> ")
            if not word.strip():
                break
            process(word)
    except (KeyboardInterrupt, EOFError):
        pass
    print("\n終了します。")


if __name__ == "__main__":
    main()
