"""Contrato real del SDK contra un transporte HTTP simulado, sin consumir la API."""

import json
import unittest
from unittest.mock import patch

from openai import AuthenticationError, OpenAI
from pydantic import ValidationError

try:
    import httpx2 as httpx
except ImportError:  # OpenAI SDK 2.x usa httpx.
    import httpx

from noticias_financieras.summarizer import AnalisisSemanal, ResumenNoticias, Summarizer


DIGEST = {
    "resumen_ejecutivo": "Semana de resultados.",
    "highlights": [], "macro_global": [], "argentina": [], "empresas": [],
    "agenda_proxima_semana": [],
}
ANALYSIS = {"analisis_fundamental": [], "oportunidades": [], "riesgos_a_monitorear": []}


class SummarizerTests(unittest.TestCase):
    def make_summarizer(self, data=None, *, status="completed", content=None, http_status=200):
        self.requests = []

        def respond(request):
            self.requests.append(request)
            if http_status != 200:
                return httpx.Response(http_status, headers={"x-request-id": "req-test"}, json={
                    "error": {"message": "Clave inválida", "type": "invalid_request_error",
                              "code": "invalid_api_key"}})
            parts = content if content is not None else [{
                "type": "output_text", "text": json.dumps(data), "annotations": []}]
            return httpx.Response(200, headers={"x-request-id": "req-test"}, json={
                "id": "resp-test", "object": "response", "created_at": 0,
                "model": "gpt-6.1-terra", "status": status,
                "error": None,
                "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
                "output": [{"id": "msg-test", "type": "message", "role": "assistant",
                            "status": "completed", "content": parts}],
                "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 5}},
            })

        client = OpenAI(api_key="sk-test-placeholder", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(client.close)
        with patch("noticias_financieras.summarizer.OpenAI", return_value=client):
            return Summarizer("gpt-6.1-terra", "high")

    def test_structured_news_and_observability(self):
        summarizer = self.make_summarizer(DIGEST)
        with self.assertLogs("noticias_financieras.summarizer", level="INFO") as logs:
            result = summarizer._call("Noticias de prueba", ResumenNoticias)
        self.assertEqual(result.resumen_ejecutivo, DIGEST["resumen_ejecutivo"])
        request = self.requests[0]
        self.assertEqual(request.url.path, "/v1/responses")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "gpt-6.1-terra")
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertIn("req-test", "\n".join(logs.output))
        self.assertNotIn("Noticias de prueba", "\n".join(logs.output))

    def test_structured_analysis(self):
        summarizer = self.make_summarizer(ANALYSIS)
        result = summarizer._call("Análisis", AnalisisSemanal)
        self.assertIsInstance(result, AnalisisSemanal)

    def test_refusal_activates_fallback(self):
        summarizer = self.make_summarizer(content=[{"type": "refusal", "refusal": "No"}])
        with self.assertLogs("noticias_financieras.summarizer", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "rechazó"):
                summarizer._call("Noticias", ResumenNoticias)

    def test_incomplete_output_is_rejected(self):
        summarizer = self.make_summarizer(status="incomplete", content=[])
        with self.assertLogs("noticias_financieras.summarizer", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "max_output_tokens"):
                summarizer._call("Noticias", ResumenNoticias)

    def test_completed_without_output_is_rejected(self):
        summarizer = self.make_summarizer(content=[])
        with self.assertLogs("noticias_financieras.summarizer", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "esquema"):
                summarizer._call("Noticias", ResumenNoticias)

    def test_invalid_schema_is_rejected_and_logged(self):
        summarizer = self.make_summarizer({"campo_incorrecto": True})
        with self.assertLogs("noticias_financieras.summarizer", level="ERROR") as logs:
            with self.assertRaises(ValidationError):
                summarizer._call("Noticias", ResumenNoticias)
        self.assertIn("ResumenNoticias", "\n".join(logs.output))
        self.assertIn("Traceback", "\n".join(logs.output))

    def test_http_error_keeps_request_id(self):
        summarizer = self.make_summarizer(http_status=401)
        with self.assertLogs("noticias_financieras.summarizer", level="ERROR") as logs:
            with self.assertRaises(AuthenticationError):
                summarizer._call("Noticias", ResumenNoticias)
        self.assertIn("status=401", "\n".join(logs.output))
        self.assertIn("req-test", "\n".join(logs.output))
