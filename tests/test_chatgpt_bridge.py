import asyncio
import tempfile
import unittest
from pathlib import Path

from chatgpt_bridge import SharedTranscript, create_server
from output.archive import TranscriptArchive


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.archive = TranscriptArchive(self.directory)
        self.path = self.archive.append_final("第一句话。")
        self.shared = SharedTranscript(self.path, self.directory)

    def test_read_then_append_without_duplicates(self):
        first = self.shared.read()
        self.assertIn("第一句话。", first["text"])
        self.assertEqual(self.shared.read(first["next_cursor"])["text"], "")
        self.archive.append_final("第二句话。")
        second = self.shared.read(first["next_cursor"])
        self.assertEqual(second["text"], "第二句话。\n\n")
        self.assertFalse(second["has_more"])

    def test_partial_utf8_write_is_not_exposed(self):
        cursor = self.shared.read()["next_cursor"]
        encoded = "未完成句子。\n\n".encode()
        with self.path.open("ab") as file:
            file.write(encoded[:4])
        self.assertEqual(self.shared.read(cursor)["text"], "")
        with self.path.open("ab") as file:
            file.write(encoded[4:])
        self.assertEqual(self.shared.read(cursor)["text"], encoded.decode())

    def test_pagination_preserves_every_sentence(self):
        for i in range(100):
            self.archive.append_final(f"第{i}句：" + "内容" * 30)
        cursor, parts = "", []
        while True:
            result = self.shared.read(cursor, 1024)
            parts.append(result["text"])
            cursor = result["next_cursor"]
            if not result["has_more"]:
                break
        self.assertEqual("".join(parts), self.path.read_bytes().decode())

    def test_invalid_cursor_and_truncation(self):
        for cursor in ("other:0", f"{self.shared.session_id}:2", f"{self.shared.session_id}:999999"):
            with self.assertRaises(ValueError):
                self.shared.read(cursor)
        cursor = self.shared.read()["next_cursor"]
        self.path.write_bytes(b"")
        with self.assertRaises(ValueError):
            self.shared.read(cursor)

    def test_only_selected_markdown_directory(self):
        private = self.directory / ".env"
        private.write_text("not shared")
        with self.assertRaises(ValueError):
            SharedTranscript(private, self.directory)
        with self.assertRaises(ValueError):
            SharedTranscript(self.path, self.directory / "other")

    def test_crlf(self):
        self.path.write_bytes("标题\r\n\r\n正文\r\n\r\n".encode())
        result = self.shared.read()
        self.assertIn("正文", result["text"])
        self.assertEqual(self.shared.read(result["next_cursor"])["text"], "")

    def test_oversized_paragraph_is_not_silently_skipped(self):
        cursor = self.shared.read()["next_cursor"]
        self.archive.append_final("长" * 1000)
        with self.assertRaises(ValueError):
            self.shared.read(cursor, 1024)
        self.assertIn("长" * 1000, self.shared.read(cursor)["text"])

    def test_mcp_http_initialize_discover_and_read(self):
        import httpx

        async def exercise():
            server = create_server(self.shared)
            app = server.streamable_http_app()
            async with server.session_manager.run():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:8765",
                    headers={"Accept": "application/json, text/event-stream"},
                ) as client:
                    async def rpc(method, params):
                        response = await client.post("/mcp", json={
                            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
                        })
                        response.raise_for_status()
                        body = response.json()
                        self.assertNotIn("error", body)
                        return body["result"]

                    initialized = await rpc("initialize", {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "bridge-test", "version": "1.0"},
                    })
                    self.assertEqual(initialized["serverInfo"]["name"], "Qwen ASR Transcript")
                    catalog = await rpc("tools/list", {})
                    self.assertEqual(len(catalog["tools"]), 1)
                    self.assertTrue(catalog["tools"][0]["annotations"]["readOnlyHint"])
                    result = await rpc("tools/call", {"name": "read_transcript", "arguments": {}})
                    self.assertFalse(result.get("isError", False))
                    self.assertIn("第一句话。", result["structuredContent"]["text"])
                    cursor = result["structuredContent"]["next_cursor"]
                    self.archive.append_final("MCP 增量测试。")
                    result = await rpc("tools/call", {
                        "name": "read_transcript", "arguments": {"cursor": cursor},
                    })
                    self.assertEqual(result["structuredContent"]["text"], "MCP 增量测试。\n\n")
        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
