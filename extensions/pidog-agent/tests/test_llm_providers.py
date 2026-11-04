#!/usr/bin/env python3
"""Vérifie la chaîne LLM : Groq → Gemini → ALLaM → OpenRouter free.

Aucun appel réseau : ``requests.post`` est remplacé par un faux transport qui
rejoue une séquence de réponses HTTP.

    cd extensions/pidog-agent && python3 -m unittest tests.test_llm_providers -v
"""

import json
import os
import sys
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(BASE))
for path in (REPO, BASE):
    if path not in sys.path:
        sys.path.insert(0, path)

import llm_providers as llm

ENV_KEYS = ("GROQ_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENROUTER_API_KEY",
            "LLM_MODEL_CLI", "LLM_MODEL", "LLM_FALLBACKS", "LLM_GEMINI_MODELS",
            "LLM_ALLAM_MODELS", "LLM_OPENROUTER_MODELS", "LLM_PROVIDERS")
STATS_KEYS = ("calls", "prompt_tokens", "completion_tokens", "cached_tokens",
              "fallbacks")


class FakeResponse:
    def __init__(self, status=200, body=b"", payload=None, headers=None):
        self.status_code = status
        self._body = body
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = body.decode() if isinstance(body, bytes) else str(body)

    def json(self):
        return self._payload

    def iter_lines(self):
        for line in self._body.split(b"\n"):
            if line.strip():
                yield line

    def close(self):
        pass


def sse(*chunks):
    """Corps SSE façon OpenAI à partir de deltas."""
    body = b""
    for chunk in chunks:
        body += b"data: " + json.dumps(chunk).encode() + b"\n\n"
    return body + b"data: [DONE]\n\n"


def delta(text=None, usage=None):
    chunk = {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
    if usage:
        chunk["usage"] = usage
    return chunk


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in ENV_KEYS}
        llm.reset_quotas()
        for key in STATS_KEYS:
            llm.STATS[key] = 0
        llm.STATS["by_target"] = {}

    def tearDown(self):
        for name, value in self.saved.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        llm.reset_quotas()

    # ------------------------------------------------------------ ordre ----
    def test_ordre_par_defaut(self):
        for name in ("GROQ_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
            os.environ[name] = "cle"
        providers = [t.provider for t in llm.chain()]
        # L'ordre des familles compte ; le nombre de modèles par famille est
        # configurable (LLM_*_MODELS) : ne pas le figer ici.
        familles = list(dict.fromkeys(providers))
        self.assertEqual(familles, ["groq", "gemini", "allam", "openrouter"])
        self.assertEqual(providers, sorted(providers, key=familles.index))
        self.assertGreaterEqual(providers.count("groq"), 2)   # modèle + repli

    def test_allam_avant_openrouter(self):
        os.environ["GROQ_KEY"] = "cle"
        providers = [t.provider for t in llm.chain()]
        self.assertLess(providers.index("allam"), providers.index("openrouter"))

    def test_force_fournisseur(self):
        os.environ["GROQ_KEY"] = "cle"
        os.environ["GEMINI_API_KEY"] = "cle"
        chain = llm.chain("gemini/gemini-3.8-flash")
        self.assertEqual(chain[0].provider, "gemini")
        self.assertEqual(chain[0].model, "gemini-3.8-flash")

    def test_sans_cle_les_fournisseurs_sont_sautes(self):
        os.environ["GROQ_KEY"] = "cle"
        usable = [t.label for t in llm.chain() if t.available()]
        self.assertTrue(usable)
        for label in usable:
            self.assertTrue(label.startswith(("groq:", "allam:")), label)

    # --------------------------------------------------------- bascule ----
    def test_bascule_sur_quota(self):
        os.environ["GROQ_KEY"] = "cle"
        os.environ["GEMINI_API_KEY"] = "cle"
        calls = []

        def fake_post(url, headers=None, json=None, stream=False, timeout=None):
            calls.append(json["model"])
            if url == llm.GROQ_URL:
                return FakeResponse(429, b'{"error":{"message":"rate limit"}}',
                                    {"error": {"message": "rate limit"}},
                                    {"retry-after": "120"})
            return FakeResponse(200, sse(
                delta("bonjour"),
                delta(usage={"prompt_tokens": 10, "completion_tokens": 2,
                             "prompt_tokens_details": {"cached_tokens": 8}})))

        with mock.patch.object(llm.requests, "post", fake_post):
            events = list(llm.stream([{"role": "user", "content": "salut"}]))

        self.assertEqual("".join(v for k, v in events if k == "text"), "bonjour")
        self.assertTrue(any(k == "info" and "bascule" in v for k, v in events))
        self.assertTrue(calls[-1].startswith("gemini-"), calls)
        stats = llm.cache_stats()
        self.assertEqual(stats["cached_tokens"], 8)
        self.assertEqual(stats["cache_hit_pct"], 80.0)
        self.assertGreaterEqual(stats["fallbacks"], 1)

    def test_cle_refusee_ecarte_le_fournisseur(self):
        os.environ["GROQ_KEY"] = "mauvaise"
        os.environ["OPENROUTER_API_KEY"] = "bonne"

        def fake_post(url, headers=None, json=None, stream=False, timeout=None):
            if url in (llm.GROQ_URL,):
                return FakeResponse(401, b'{"error":{"message":"Invalid API Key"}}',
                                    {"error": {"message": "Invalid API Key"}})
            return FakeResponse(200, sse(delta("ok")))

        with mock.patch.object(llm.requests, "post", fake_post):
            events = list(llm.stream([{"role": "user", "content": "salut"}]))

        self.assertEqual("".join(v for k, v in events if k == "text"), "ok")
        self.assertTrue(any(k == "info" and "clé refusée" in v for k, v in events))
        self.assertIn("groq", llm._dead_providers)
        self.assertIn("allam", llm._dead_providers)

    def test_aucune_cle_message_explicite(self):
        events = list(llm.stream([{"role": "user", "content": "salut"}]))
        self.assertEqual(events[0][0], "error")
        self.assertIn("GROQ_KEY", events[0][1])

    def test_cooldown_evite_de_retaper(self):
        os.environ["GROQ_KEY"] = "cle"
        os.environ["GEMINI_API_KEY"] = "cle"
        hits = []

        def fake_post(url, headers=None, json=None, stream=False, timeout=None):
            hits.append(url)
            if url == llm.GROQ_URL:
                return FakeResponse(429, b'{"error":{"message":"rate limit"}}',
                                    {"error": {"message": "rate limit"}},
                                    {"retry-after": "120"})
            return FakeResponse(200, sse(delta("ok")))

        with mock.patch.object(llm.requests, "post", fake_post):
            list(llm.stream([{"role": "user", "content": "un"}]))
            before = hits.count(llm.GROQ_URL)
            list(llm.stream([{"role": "user", "content": "deux"}]))
        self.assertEqual(hits.count(llm.GROQ_URL), before)

    def test_complete_non_stream(self):
        os.environ["GROQ_KEY"] = "cle"
        payload = {"choices": [{"message": {"content": "pong"},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 5, "completion_tokens": 1}}

        def fake_post(url, headers=None, json=None, stream=False, timeout=None):
            return FakeResponse(200, payload=payload)

        with mock.patch.object(llm.requests, "post", fake_post):
            data, error = llm.complete([{"role": "user", "content": "ping"}])
        self.assertIsNone(error)
        self.assertEqual(data["choices"][0]["message"]["content"], "pong")
        self.assertEqual(llm.cache_stats()["prompt_tokens"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
