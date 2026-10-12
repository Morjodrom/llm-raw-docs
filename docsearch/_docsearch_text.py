"""Explicitly initialized, local Russian morphology."""
import hashlib
import importlib.metadata
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from _docsearch_models import LemmatizerInitializationError

TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
RUSSIAN = re.compile(r"[а-яё]+")


def words(text):
    return TOKEN.findall(unicodedata.normalize("NFC", text).lower())


class TextProcessor:
    def __init__(self, config):
        try:
            import pymorphy3
            import pymorphy3_dicts_ru
            self.morph = pymorphy3.MorphAnalyzer(lang="ru", path=pymorphy3_dicts_ru.get_path())
            digest = hashlib.sha256()
            root = Path(pymorphy3_dicts_ru.get_path())
            for path in sorted(root.iterdir()):
                if path.is_file():
                    digest.update(path.name.encode())
                    digest.update(path.read_bytes())
            self.fingerprint = {
                "lemmatizer": importlib.metadata.version("pymorphy3"),
                "dictionary": importlib.metadata.version("pymorphy3-dicts-ru"),
                "dictionary_sha256": digest.hexdigest(),
                "parser": importlib.metadata.version("markdown-it-py"),
                "pipeline": config.processing_version,
                "tokenizer": "unicode61 remove_diacritics 0",
                "normalization": "NFC/lower/known-russian-top-analysis/word-v1",
            }
            self.lemma = lru_cache(maxsize=config.lemma_cache_size)(self._lemma)
            if self.lemma("ошибки") != "ошибка":
                raise RuntimeError("Russian dictionary self-check failed")
        except Exception as exc:
            raise LemmatizerInitializationError(f"Cannot initialize local Russian dictionaries: {exc}") from exc

    def _lemma(self, word):
        if not RUSSIAN.fullmatch(word):
            return word
        known = [p for p in self.morph.parse(word) if p.is_known]
        return known[0].normal_form if known else word

    def lemmas(self, text):
        return [self.lemma(w) for w in words(text)]

    def original(self, text):
        return " ".join(words(text))

    def transformed(self, text):
        return " ".join(self.lemmas(text))

    def close(self):
        self.lemma.cache_clear()
        self.morph = None
