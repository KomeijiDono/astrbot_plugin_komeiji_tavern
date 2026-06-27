import asyncio
import json
import random
import sqlite3
import tempfile
import unittest
import base64
import struct
import zlib
import io
import zipfile
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_komeiji_tavern.constants import API_PREFIX, PLUGIN_ID, PLUGIN_VERSION
from astrbot_plugin_komeiji_tavern.importers import detect_kind, export_document, parse_binary_payload, parse_payload, preview_import
from astrbot_plugin_komeiji_tavern.documents import validate_document
from astrbot_plugin_komeiji_tavern.lore import LoreScanner, normalize_entries
from astrbot_plugin_komeiji_tavern.models import Position, ScanResult
from astrbot_plugin_komeiji_tavern.prompt_builder import PromptBuilder, estimate_tokens
from astrbot_plugin_komeiji_tavern.qq_delivery import split_forward_text
from astrbot_plugin_komeiji_tavern.storage import TavernStorage
from astrbot_plugin_komeiji_tavern.service import TavernService
from astrbot_plugin_komeiji_tavern.web import TavernWebApi
from astrbot_plugin_komeiji_tavern.main import KomeijiTavernPlugin, _flatten_config
from astrbot_plugin_komeiji_tavern.illustration import OmniDrawBridge
from astrbot_plugin_komeiji_tavern.export_utils import (
    build_document_archive,
    build_session_backup,
    document_download,
    safe_filename,
)
from astrbot.api.message_components import At, Nodes, Plain
from astrbot.core.message.message_event_result import MessageEventResult


def run(coro):
    return asyncio.run(coro)


def entry(uid, keys, content, **extra):
    return {"uid": uid, "key": keys, "content": content, **extra}


class ExportTests(unittest.TestCase):
    @staticmethod
    def read_zip(content):
        return zipfile.ZipFile(io.BytesIO(content))

    def test_safe_filename_removes_windows_invalid_characters(self):
        self.assertEqual(safe_filename('a<b>:c/'), 'a_b__c_')
        self.assertEqual(safe_filename('... ', 'fallback'), 'fallback')

    def test_individual_download_preserves_unknown_fields(self):
        document = {
            "id": "doc-1", "kind": "character", "name": "Alice",
            "raw": {"name": "old", "unknown": {"keep": True}},
            "data": {"name": "Alice"},
        }
        payload = document_download(document)
        decoded = json.loads(base64.b64decode(payload["base64"]))
        self.assertEqual(payload["filename"], "Alice.json")
        self.assertTrue(decoded["unknown"]["keep"])
        self.assertEqual(decoded["name"], "Alice")

    def test_document_archive_groups_documents_and_writes_manifest(self):
        content = build_document_archive([
            {"id": "char-123456", "kind": "character", "name": "Alice", "raw": {}, "data": {"name": "Alice"}},
            {"id": "book-123456", "kind": "lorebook", "name": "World", "raw": {}, "data": {"entries": []}},
        ])
        with self.read_zip(content) as archive:
            names = archive.namelist()
            self.assertIn("角色卡/Alice-char-123.json", names)
            self.assertIn("世界书/World-book-123.json", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["count"], 2)
            self.assertEqual(manifest["version"], PLUGIN_VERSION)

    def test_session_backup_keeps_exact_messages_preview_and_state(self):
        messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hello"}]
        preview = {"messages": messages, "warnings": ["test"]}
        state = {"turn": 7, "history_summary": {"content": "summary"}}
        with self.read_zip(build_session_backup("s1", state, preview)) as archive:
            self.assertEqual(json.loads(archive.read("messages.json")), messages)
            self.assertEqual(json.loads(archive.read("preview.json")), preview)
            self.assertEqual(json.loads(archive.read("session-state.json")), state)
            self.assertEqual(json.loads(archive.read("manifest.json"))["message_count"], 2)


class CoreTests(unittest.TestCase):
    def test_grouped_config_flattens_without_losing_values(self):
        grouped = {
            "context_config": {"history_max_messages": 12},
            "qq_forward_config": {"qq_forward_nodes_per_batch": 12},
            "summary_config": {"summary_enabled": True},
            "lifecycle_config": {"session_retention_days": 30},
        }
        flattened = _flatten_config(grouped)
        self.assertEqual(flattened["history_max_messages"], 12)
        self.assertEqual(flattened["qq_forward_nodes_per_batch"], 12)
        self.assertTrue(flattened["summary_enabled"])
        self.assertEqual(flattened["session_retention_days"], 30)

    def test_qq_direct_split_sends_plain_messages_and_clears_result(self):
        class Result:
            def __init__(self):
                self.chain = [Plain("中" * 3200)]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            def __init__(self):
                self.result = Result()
                self.sent = []

            @staticmethod
            def get_platform_name():
                return "aiocqhttp"

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            async def send(self, chain):
                self.sent.append(chain)

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "qq_direct_split_enabled": True,
            "qq_direct_message_chars": 1500,
            "qq_direct_send_interval_ms": 0,
            "qq_forward_trigger_chars": 100,
        }
        event = Event()
        run(plugin.deliver_qq_long_reply(event))
        self.assertIsNone(event.result)
        self.assertEqual([len(chain.chain[0].text) for chain in event.sent], [1500, 1500, 200])
        self.assertTrue(all(isinstance(chain.chain[0], Plain) for chain in event.sent))

    def test_qq_direct_split_retries_failed_chunk(self):
        class Result:
            chain = [Plain("中" * 2200)]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            def __init__(self):
                self.result = Result()
                self.calls = []
                self.successful = []

            @staticmethod
            def get_platform_name():
                return "aiocqhttp"

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            async def send(self, chain):
                length = len(chain.chain[0].text)
                self.calls.append(length)
                if len(self.calls) == 2:
                    raise RuntimeError("temporary failure")
                self.successful.append(length)

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "qq_direct_split_enabled": True,
            "qq_direct_message_chars": 1000,
            "qq_direct_send_interval_ms": 0,
            "qq_direct_retry_count": 1,
            "qq_direct_retry_delay_ms": 0,
            "qq_forward_trigger_chars": 100,
        }
        event = Event()
        run(plugin.deliver_qq_long_reply(event))
        self.assertIsNone(event.result)
        self.assertEqual(event.calls, [1000, 1000, 1000, 200])
        self.assertEqual(event.successful, [1000, 1000, 200])

    def test_qq_forward_text_split_preserves_content_and_limit(self):
        text = ("第一段。\n" * 900) + ("x" * 3000)
        chunks = split_forward_text(text, 2500)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(0 < len(chunk) <= 2500 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    def test_qq_forward_text_split_counts_unicode_characters(self):
        chunks = split_forward_text("中" * 5001, 2500)
        self.assertEqual([len(chunk) for chunk in chunks], [2500, 2500, 1])

    def test_qq_forward_sends_multiple_bounded_batches(self):
        class Result:
            chain = [Plain("中" * 750)]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            def __init__(self):
                self.result = Result()
                self.sent = []

            @staticmethod
            def get_platform_name():
                return "aiocqhttp"

            @staticmethod
            def get_self_id():
                return "123"

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            async def send(self, chain):
                self.sent.append(chain)

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "qq_forward_split_enabled": True,
            "qq_forward_trigger_chars": 100,
            "qq_forward_node_chars": 100,
            "qq_forward_nodes_per_batch": 3,
            "qq_forward_batch_interval_ms": 0,
        }
        event = Event()
        run(plugin.deliver_qq_long_reply(event))
        self.assertIsNone(event.result)
        self.assertEqual(len(event.sent), 3)
        self.assertEqual([len(chain.chain[0].nodes) for chain in event.sent], [3, 3, 2])
        self.assertTrue(all(isinstance(chain.chain[0], Nodes) for chain in event.sent))

    def test_qq_forward_failure_falls_back_without_repeating_sent_batch(self):
        text = "中" * 700

        class Result:
            chain = [Plain(text)]

            @staticmethod
            def is_llm_result():
                return True

        class Event:
            def __init__(self):
                self.result = Result()
                self.forward_attempts = 0
                self.successful = []

            @staticmethod
            def get_platform_name():
                return "aiocqhttp"

            @staticmethod
            def get_self_id():
                return "123"

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            async def send(self, chain):
                component = chain.chain[0]
                if isinstance(component, Nodes):
                    self.forward_attempts += 1
                    if self.forward_attempts == 2:
                        raise RuntimeError("forward failed")
                self.successful.append(chain)

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "qq_forward_split_enabled": True,
            "qq_forward_fallback_enabled": True,
            "qq_forward_trigger_chars": 100,
            "qq_forward_node_chars": 100,
            "qq_forward_nodes_per_batch": 3,
            "qq_forward_batch_interval_ms": 0,
            "qq_direct_send_interval_ms": 0,
            "qq_direct_retry_count": 0,
        }
        event = Event()
        run(plugin.deliver_qq_long_reply(event))
        self.assertIsNone(event.result)
        self.assertEqual(event.forward_attempts, 2)
        sent_forward = "".join(node.content[0].text for node in event.successful[0].chain[0].nodes)
        fallback = "".join(chain.chain[0].text for chain in event.successful[1:])
        self.assertEqual(sent_forward, text[:300])
        self.assertEqual(fallback, text[300:])

    def test_tavern_preview_yields_local_result_without_requesting_llm(self):
        class Storage:
            @staticmethod
            def get_preview(session_id):
                return {"session_id": session_id, "messages": [{"role": "user", "content": "hello"}]}

        class Event:
            def __init__(self):
                self.extras = {}

            @staticmethod
            def plain_result(text):
                return MessageEventResult().message(text)

            def set_extra(self, key, value):
                self.extras[key] = value

            @staticmethod
            def request_llm(**_):
                raise AssertionError("preview must not request the LLM")

        async def collect(generator):
            return [item async for item in generator]

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.storage = Storage()
        plugin._session_id = lambda _event: "test-session"
        event = Event()
        results = run(collect(plugin.tavern(event, "preview", "")))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], MessageEventResult)
        self.assertTrue(event.extras["_kt_force_long_delivery"])
        payload = json.loads(results[0].chain[0].text)
        self.assertEqual(payload["session_id"], "test-session")
        self.assertEqual(payload["messages"][0]["content"], "hello")

    def test_mentioned_tavern_reset_handles_unstripped_slash(self):
        class Service:
            def __init__(self):
                self.reset_ids = []

            async def reset_session(self, session_id):
                self.reset_ids.append(session_id)

        class Event:
            def __init__(self, target="bot-self-id"):
                self.target = target

            @staticmethod
            def get_self_id():
                return "bot-self-id"

            def get_messages(self):
                return [At(qq=self.target), Plain("/tavern reset")]

            @staticmethod
            def get_message_str():
                return "/tavern reset"

            @staticmethod
            def plain_result(text):
                return MessageEventResult().message(text)

        async def collect(generator):
            return [item async for item in generator]

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.service = Service()
        plugin._session_id = lambda _event: "test-session"

        results = run(collect(plugin.tavern_mentioned(Event())))
        self.assertEqual(plugin.service.reset_ids, ["test-session"])
        self.assertEqual(len(results), 1)
        self.assertIn("状态已清除", results[0].chain[0].text)

        ignored = run(collect(plugin.tavern_mentioned(Event("123456"))))
        self.assertEqual(ignored, [])
        self.assertEqual(plugin.service.reset_ids, ["test-session"])

    def test_forced_preview_result_uses_long_delivery_without_llm_result_type(self):
        class Result:
            chain = [Plain("中" * 250)]

            @staticmethod
            def is_llm_result():
                return False

        class Event:
            def __init__(self):
                self.result = Result()
                self.sent = []
                self.extras = {"_kt_force_long_delivery": True}

            @staticmethod
            def get_platform_name():
                return "aiocqhttp"

            @staticmethod
            def get_self_id():
                return "123"

            def get_extra(self, key):
                return self.extras.get(key)

            def set_extra(self, key, value):
                self.extras[key] = value

            def get_result(self):
                return self.result

            def clear_result(self):
                self.result = None

            async def send(self, chain):
                self.sent.append(chain)

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "qq_forward_split_enabled": True,
            "qq_forward_trigger_chars": 100,
            "qq_forward_node_chars": 100,
            "qq_forward_nodes_per_batch": 6,
            "qq_forward_batch_interval_ms": 0,
        }
        event = Event()
        run(plugin.deliver_qq_long_reply(event))
        self.assertIsNone(event.result)
        self.assertEqual(len(event.sent), 1)
        self.assertIsInstance(event.sent[0].chain[0], Nodes)
        self.assertEqual([len(node.content[0].text) for node in event.sent[0].chain[0].nodes], [100, 100, 50])

    def test_selective_logic_and_recursion(self):
        data = {"entries": {
            "1": entry(1, ["gate"], "dragon appears", keysecondary=["open"], selective=True, selectiveLogic=0),
            "2": entry(2, ["dragon"], "recursive result"),
            "3": entry(3, ["gate"], "must not activate", keysecondary=["blocked"], selective=True, selectiveLogic=2),
        }}
        entries = normalize_entries(data)
        result = run(LoreScanner(max_recursion_steps=3).scan(
            entries, [{"role": "user", "content": "open the gate, blocked"}],
            {"turn": 0, "effects": {}}, rng=random.Random(1),
        ))
        self.assertEqual([item.entry.uid for item in result.activated], ["1", "2"])
        self.assertEqual(result.activated[1].recursion_step, 1)


    def test_sticky_cooldown_delay_lifecycle(self):
        scanner = LoreScanner(max_recursion_steps=0)
        entries = normalize_entries({"entries": [entry("x", ["hit"], "value", sticky=2, cooldown=2, delay=1)]})
        state = {"turn": 0, "effects": {}}
        self.assertFalse(run(scanner.scan(entries, [{"role": "user", "content": "hit"}], state)).activated)
        self.assertEqual(run(scanner.scan(entries, [{"role": "user", "content": "hit"}], state)).activated[0].reason, "keyword")
        self.assertEqual(run(scanner.scan(entries, [{"role": "user", "content": "none"}], state)).activated[0].reason, "sticky")
        self.assertFalse(run(scanner.scan(entries, [{"role": "user", "content": "hit"}], state)).activated)
        self.assertFalse(run(scanner.scan(entries, [{"role": "user", "content": "hit"}], state)).activated)
        self.assertTrue(run(scanner.scan(entries, [{"role": "user", "content": "hit"}], state)).activated)


    def test_probability_and_constant(self):
        entries = normalize_entries({"entries": [
            entry("never", ["x"], "no", probability=0),
            entry("always", [], "yes", constant=True),
        ]})
        result = run(LoreScanner(max_recursion_steps=0).scan(
            entries, [{"role": "user", "content": "x"}], {"turn": 0, "effects": {}}, rng=random.Random(1)
        ))
        self.assertEqual([item.entry.uid for item in result.activated], ["always"])


    def test_prompt_positions_examples_and_budget(self):
        lore_entries = normalize_entries({"entries": [
            entry("before", [], "before lore", constant=True, position=int(Position.BEFORE_CHARACTER)),
            entry("depth", [], "depth lore", constant=True, position=int(Position.AT_DEPTH), depth=1, role="user"),
        ]})
        lore = ScanResult()
        from astrbot_plugin_komeiji_tavern.models import ActivatedEntry
        lore.activated = [ActivatedEntry(item, "constant") for item in lore_entries]
        builder = PromptBuilder(context_budget=2048, output_reserve=256)
        result = builder.build(
            original_system="base", contexts=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "reply"}],
            current_prompt="now", preset={}, character={"name": "A", "description": "card", "mes_example": "User: hello\nA: hi"},
            persona="persona", lore=lore, values={"user": "User", "char": "A"},
        )
        self.assertLess(result.system_prompt.index("before lore"), result.system_prompt.index("card"))
        self.assertTrue(any(item.get("_kt_injected") == "lore:depth" and item["role"] == "user" for item in result.contexts))
        self.assertTrue(any(item.get("_kt_example") for item in result.contexts))
        self.assertEqual(result.messages[-1], {"role": "user", "content": "now"})


    def test_budget_trims_old_history_before_character(self):
        builder = PromptBuilder(
            context_budget=2048,
            output_reserve=256,
            history_first_trimming=True,
            history_keep_recent_messages=2,
        )
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "old " * 300}
            for index in range(8)
        ]
        result = builder.build(
            original_system="main",
            contexts=history,
            current_prompt="now",
            preset={},
            character={"description": "character identity " * 40},
            persona="persona identity",
            lore=ScanResult(),
            values={"user": "User", "char": "Character"},
        )
        self.assertIn("history:oldest", result.dropped)
        self.assertNotIn("character", result.dropped)
        self.assertIn("character identity", result.system_prompt)
        self.assertGreaterEqual(len(result.contexts), 2)

    def test_history_message_limit_keeps_latest_messages(self):
        builder = PromptBuilder(history_max_messages=4)
        history = [
            {"role": "user", "content": f"message-{index}"}
            for index in range(10)
        ]
        result = builder.build(
            original_system="main",
            contexts=history,
            current_prompt="now",
            preset={},
            character=None,
            persona="",
            lore=ScanResult(),
            values={"user": "User", "char": "Character"},
        )
        sent_history = [item["content"] for item in result.contexts]
        self.assertEqual(sent_history, ["message-6", "message-7", "message-8", "message-9"])
        self.assertEqual(result.dropped.count("history:max_messages"), 6)

    def test_session_summary_appends_to_static_summary_block(self):
        result = PromptBuilder().build(
            original_system="main", contexts=[], current_prompt="now",
            preset={"summary": "static summary"}, character=None, persona="",
            lore=ScanResult(), values={}, session_summary="rolling summary",
        )
        summary = next(block for block in result.blocks if block.identifier == "summary")
        self.assertIn("static summary", summary.content)
        self.assertIn("rolling summary", summary.content)

    def test_storage_persistence_and_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first = TavernStorage(path)
            document_id = first.put_document("lorebook", "Book", {"entries": []})
            first.bind("session", "s1", "lorebook", document_id)
            first.save_session("s1", {"turn": 7, "effects": {"x": {"sticky_until": 9}}})
            second = TavernStorage(path)
            self.assertEqual(second.get_session("s1")["turn"], 7)
            self.assertEqual(second.resolve_bindings("lorebook", [("session", "s1")])[0]["id"], document_id)

    def test_storage_cleanup_uses_independent_cutoffs(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            document_id = storage.put_document("preset", "Keep", {})
            storage.bind("global", "*", "preset", document_id)
            with patch("astrbot_plugin_komeiji_tavern.storage.time.time", return_value=100.0):
                storage.save_session("old", {"turn": 1})
                storage.save_preview("old", {"messages": []})
            with patch("astrbot_plugin_komeiji_tavern.storage.time.time", return_value=300.0):
                storage.save_session("new", {"turn": 2})
                storage.save_preview("new", {"messages": []})
            with patch("astrbot_plugin_komeiji_tavern.storage.time.time", return_value=200.0):
                storage.save_session("boundary", {"turn": 3})
            with patch("astrbot_plugin_komeiji_tavern.storage.time.time", return_value=350.0):
                storage.save_preview("boundary", {"messages": ["keep"]})
            deleted = storage.cleanup_expired(session_cutoff=200.0, preview_cutoff=350.0)
            self.assertEqual(deleted, {"sessions": 1, "previews": 2})
            self.assertEqual(storage.get_session("new")["turn"], 2)
            self.assertEqual(storage.get_session("boundary")["turn"], 3)
            self.assertIsNotNone(storage.get_preview("boundary"))
            self.assertIsNotNone(storage.get_document(document_id))
            self.assertEqual(len(storage.list_bindings()), 1)
            conn = sqlite3.connect(storage.path)
            try:
                indexes = {row[1] for row in conn.execute("pragma index_list(sessions)")}
            finally:
                conn.close()
            self.assertIn("idx_sessions_updated_at", indexes)

    def test_binding_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            global_id = storage.put_document("preset", "Global", {})
            persona_id = storage.put_document("preset", "Persona", {})
            session_id = storage.put_document("preset", "Session", {})
            storage.bind("global", "*", "preset", global_id)
            storage.bind("persona", "p1", "preset", persona_id)
            storage.bind("session", "s1", "preset", session_id)
            service = TavernService(storage, object(), {})
            selected = service._bound_one("preset", [("global", "*"), ("session", "s1"), ("persona", "p1")])
            self.assertEqual(selected["id"], session_id)

    def test_unbound_single_resource_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            storage.put_document("character", "Unbound", {"name": "No"})
            service = TavernService(storage, object(), {})
            self.assertIsNone(service._bound_one("character", [("global", "*")]))

    def test_binding_listing_and_document_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            document_id = storage.put_document("character", "Alice", {"data": {"name": "Alice"}})
            storage.bind("session", "s1", "character", document_id)
            listed = storage.list_bindings(scope_type="session", scope_id="s1")
            self.assertEqual(listed[0]["target_name"], "Alice")
            normalized, errors, _ = validate_document("character", {"data": {"name": "Alice"}})
            self.assertFalse(errors)
            self.assertEqual(normalized["_komeiji_tavern_version"], 2)

    def test_character_override_policy(self):
        builder = PromptBuilder()
        result = builder.build(
            original_system="base", contexts=[], current_prompt="hi",
            preset={"main_prompt": "preset", "allow_character_main_override": True,
                "allow_character_phi_override": False, "post_history_instructions": "preset phi"},
            character={"system_prompt": "card main", "post_history_instructions": "card phi"},
            persona="", lore=ScanResult(), values={"user": "User", "char": "A"},
        )
        self.assertIn("card main", result.system_prompt)
        self.assertIn("preset phi", result.contexts[0]["content"])

    def test_simulation_activates_lore_without_persisting_state(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            lore_id = storage.put_document("lorebook", "Book", {
                "entries": [entry("x", ["dragon"], "lore content", sticky=3)]
            })
            storage.bind("global", "*", "preset", preset_id)
            storage.bind("session", "s1", "lorebook", lore_id)
            before = storage.get_session("s1")
            result = run(TavernService(storage, object(), {}).simulate({
                "session_id": "s1", "prompt": "dragon", "system_prompt": "system"
            }))
            self.assertEqual(result["activated"][0]["uid"], "x")
            self.assertFalse(result["state_persisted"])
            self.assertEqual(storage.get_session("s1"), before)

    def test_simulation_collects_bound_materials_like_real_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            material_id = storage.put_document("material", "Material", {
                "entries": [entry("material-x", ["spark"], "material content", vectorized=False)]
            })
            storage.bind("global", "*", "preset", preset_id)
            storage.bind("session", "s1", "material", material_id)
            result = run(TavernService(storage, object(), {}).simulate({
                "session_id": "s1", "prompt": "spark", "system_prompt": "system"
            }))
            self.assertEqual(result["activated"][0]["uid"], "material-x")
            self.assertEqual(result["activated"][0]["content"], "material content")

    def test_simulation_warns_when_scope_has_no_preset(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            result = run(TavernService(storage, object(), {}).simulate({"prompt": "hello"}))
            self.assertEqual(result["messages"], [{"role": "user", "content": "hello"}])
            self.assertTrue(any("没有绑定提示词预设" in item for item in result["warnings"]))

    def test_bound_session_is_available_without_astrbot_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            document_id = storage.put_document("character", "Alice", {"data": {"name": "Alice"}})
            session_id = "default:GroupMessage:123_456"
            storage.bind("session", session_id, "character", document_id)
            api = object.__new__(TavernWebApi)
            api.storage = storage
            items = api._merge_bound_conversations([])
            self.assertEqual(items[0]["id"], session_id)
            self.assertEqual(items[0]["source"], "binding")

    def test_runtime_constants_match_public_metadata(self):
        metadata = (Path(__file__).parents[1] / "metadata.yaml").read_text(encoding="utf-8")
        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
        package = json.loads((Path(__file__).parents[1] / "web" / "package.json").read_text(encoding="utf-8"))
        self.assertIn(f"name: {PLUGIN_ID}", metadata)
        self.assertIn(f"version: {PLUGIN_VERSION}", metadata)
        self.assertIn(f"version-{PLUGIN_VERSION}", readme)
        self.assertIn(f"## {PLUGIN_VERSION}：", changelog)
        self.assertEqual(package["version"], PLUGIN_VERSION)
        self.assertEqual(API_PREFIX, f"/{PLUGIN_ID}/v1")
        self.assertEqual(TavernWebApi.PREFIX, API_PREFIX)


    def test_import_round_trip(self):
        payload = {"entries": {"0": entry(0, ["key"], "content", extensions={"sticky": 2})}}
        self.assertEqual(detect_kind(payload), "lorebook")
        self.assertEqual(preview_import(payload)["count"], 1)
        self.assertEqual(preview_import(payload, file_name="MYGO_Mujica.json")["name"], "MYGO_Mujica")
        document = {"raw": json.loads(json.dumps(payload)), "data": payload}
        self.assertEqual(export_document(document), payload)

    def test_export_preserves_worldbook_shape_and_unknown_fields(self):
        raw = {"entries": {"7": {"uid": 7, "content": "old", "extensions": {"unknown": 1}}},
            "unknown_root": True}
        document = {"raw": raw, "data": {"_komeiji_tavern_version": 2,
            "entries": [{"uid": 7, "content": "new"}]}}
        exported = export_document(document)
        self.assertIsInstance(exported["entries"], dict)
        self.assertEqual(exported["entries"]["7"]["content"], "new")
        self.assertEqual(exported["entries"]["7"]["extensions"]["unknown"], 1)
        self.assertNotIn("_komeiji_tavern_version", exported)

    def test_plain_text_prompt_import(self):
        data = parse_payload('"第一行\n第二行"', "System_prompt.txt")
        parsed = preview_import(data)
        normalized, errors, warnings = validate_document(parsed["kind"], data)
        self.assertEqual(parsed["kind"], "preset")
        self.assertEqual(parsed["name"], "System_prompt")
        self.assertEqual(normalized["main_prompt"], "第一行\n第二行")
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_png_character_import(self):
        card = {"spec": "chara_card_v2", "data": {"name": "Alice", "first_mes": "Hello"}}
        text = b"chara\x00" + base64.b64encode(json.dumps(card).encode())
        chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text))
        end = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
        png = b"\x89PNG\r\n\x1a\n" + chunk + end
        parsed = parse_binary_payload(base64.b64encode(png).decode(), "card.png")
        self.assertEqual(parsed["data"]["name"], "Alice")


class _FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class _FakeEvent:
    def __init__(self):
        self.sent = []
        self.extras = {}

    async def send(self, chain):
        self.sent.append(chain)

    def chain_result(self, components):
        return type("R", (), {"chain": list(components)})()

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


class _FakeOmniDraw:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def generate_images_for_plugin(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeContext:
    def __init__(self, star=None):
        self._star = star

    def get_registered_star(self, name):
        return self._star


class IllustrationTests(unittest.TestCase):
    def test_build_prompt_truncates_and_prefixes(self):
        bridge = OmniDrawBridge(_FakeContext(), {
            "illustration_enabled": True,
            "illustration_max_text_chars": 50,
            "illustration_prompt_prefix": "roleplay scene,",
        })
        self.assertEqual(bridge._build_prompt("x" * 200), "roleplay scene, " + "x" * 50)
        bridge.config["illustration_prompt_prefix"] = ""
        self.assertEqual(bridge._build_prompt("hello"), "hello")
        self.assertEqual(bridge._build_prompt(""), "")

    def test_maybe_illustrate_skips_when_disabled(self):
        bridge = OmniDrawBridge(_FakeContext(_FakeOmniDraw({"success": True})), {"illustration_enabled": False})
        run(bridge.maybe_illustrate(_FakeEvent(), _FakeResponse("a" * 100)))
        self.assertEqual(bridge._tasks, set())

    def test_maybe_illustrate_skips_when_omnidraw_missing(self):
        bridge = OmniDrawBridge(_FakeContext(None), {"illustration_enabled": True})
        run(bridge.maybe_illustrate(_FakeEvent(), _FakeResponse("a" * 100)))
        self.assertEqual(bridge._tasks, set())

    def test_maybe_illustrate_skips_short_text(self):
        bridge = OmniDrawBridge(_FakeContext(_FakeOmniDraw({"success": True})), {"illustration_enabled": True})
        run(bridge.maybe_illustrate(_FakeEvent(), _FakeResponse("短文本")))
        self.assertEqual(bridge._tasks, set())

    def test_run_sends_image_on_success(self):
        with tempfile.TemporaryDirectory() as d:
            img_path = Path(d) / "a.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            omni = _FakeOmniDraw({"success": True, "images": [{"file_path": str(img_path)}]})
            bridge = OmniDrawBridge(_FakeContext(), {"illustration_enabled": True, "illustration_mode": "text2img"})
            event = _FakeEvent()
            run(bridge._run(event, omni, "prompt", consume=False, semaphore=None))
            self.assertEqual(len(event.sent), 1)
            self.assertEqual(omni.calls[0]["prompt"], "prompt")
            self.assertEqual(omni.calls[0]["event"], None)
            self.assertFalse(omni.calls[0]["record_usage"])

    def test_run_passes_event_when_consuming_quota(self):
        with tempfile.TemporaryDirectory() as d:
            img_path = Path(d) / "a.png"
            img_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            omni = _FakeOmniDraw({"success": True, "images": [{"file_path": str(img_path)}]})
            bridge = OmniDrawBridge(_FakeContext(), {"illustration_enabled": True})
            event = _FakeEvent()
            run(bridge._run(event, omni, "prompt", consume=True, semaphore=None))
            self.assertIs(omni.calls[0]["event"], event)
            self.assertTrue(omni.calls[0]["record_usage"])

    def test_run_silent_on_failure(self):
        omni = _FakeOmniDraw({"success": False, "message": "boom"})
        bridge = OmniDrawBridge(_FakeContext(), {"illustration_enabled": True})
        event = _FakeEvent()
        run(bridge._run(event, omni, "prompt", consume=False, semaphore=None))
        self.assertEqual(event.sent, [])

    def test_run_silent_when_no_images(self):
        omni = _FakeOmniDraw({"success": True, "images": []})
        bridge = OmniDrawBridge(_FakeContext(), {"illustration_enabled": True})
        event = _FakeEvent()
        run(bridge._run(event, omni, "prompt", consume=False, semaphore=None))
        self.assertEqual(event.sent, [])

    def test_data_url_uses_declared_mime_and_validates_base64(self):
        bridge = OmniDrawBridge(_FakeContext(), {})
        encoded = base64.b64encode(b"\xff\xd8\xffjpeg").decode()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "astrbot_plugin_komeiji_tavern.illustration.Path.home",
                return_value=Path(directory),
            ):
                saved = bridge._save_data_url(f"data:image/jpeg;base64,{encoded}")
                self.assertIsNotNone(saved)
                self.assertEqual(Path(saved).suffix, ".jpg")
                self.assertEqual(Path(saved).read_bytes(), b"\xff\xd8\xffjpeg")
                self.assertIsNone(bridge._save_data_url("data:image/png;base64,not-valid!"))

    def test_illustration_is_dispatched_after_message_sent(self):
        class Illustration:
            def __init__(self):
                self.calls = []

            async def maybe_illustrate_text(self, event, text):
                self.calls.append((event, text))

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {"illustration_enabled": True, "status_bar_enabled": False}
        plugin.illustration = Illustration()
        event = _FakeEvent()
        response = _FakeResponse("reply text for illustration")

        run(plugin.on_llm_response(event, response))
        self.assertEqual(plugin.illustration.calls, [])
        self.assertEqual(event.get_extra("_kt_illustration_text"), response.completion_text)

        run(plugin.after_message_sent(event))
        self.assertEqual(plugin.illustration.calls, [(event, response.completion_text)])
        self.assertEqual(event.get_extra("_kt_illustration_text"), "")


class _FakeStar:
    def __init__(self, inst):
        self.star_cls = inst


class _FakeEmbeddingContext:
    def __init__(self, providers):
        self._providers = providers

    def get_all_embedding_providers(self):
        return self._providers


class _FakeEmbeddingProvider:
    def __init__(self, pid, vectors):
        self.provider_config = {"id": pid}
        self._vectors = vectors
        self.calls = []

    async def get_embedding(self, text):
        self.calls.append(text)
        return self._vectors.get(text, [0.0, 0.0])


class _FakeMemoryProvider:
    def __init__(self, text='[{"category":"preference","content":"User likes tea"}]', *, error=None):
        self.provider_config = {"id": "memory"}
        self.text = text
        self.error = error
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return _FakeSummaryResponse(self.text)


class _FakeMemoryContext(_FakeEmbeddingContext):
    def __init__(self, embedding_provider, memory_provider):
        super().__init__([embedding_provider])
        self.memory_provider = memory_provider

    def get_provider_by_id(self, provider_id):
        return self.memory_provider if provider_id == self.memory_provider.provider_config["id"] else None

    def get_using_provider(self, _session_id):
        return self.memory_provider


class _FakeSummaryResponse:
    def __init__(self, text):
        self.completion_text = text


class _FakeSummaryProvider:
    def __init__(self, pid="summary", *, error=None):
        self.provider_config = {"id": pid}
        self.error = error
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return _FakeSummaryResponse(f"summary-{len(self.calls)}")


class _SlowSummaryProvider(_FakeSummaryProvider):
    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(2)
        return _FakeSummaryResponse("late")


class _FakeSummaryContext:
    def __init__(self, provider):
        self.provider = provider

    def get_provider_by_id(self, provider_id):
        return self.provider if self.provider.provider_config["id"] == provider_id else None

    def get_using_provider(self, _session_id):
        return self.provider


class SummaryCompressionTests(unittest.TestCase):
    @staticmethod
    def messages(count):
        return [
            {"role": "user" if index % 2 == 0 else "assistant", "content": f"message-{index}"}
            for index in range(count)
        ]

    def test_does_not_trigger_before_threshold(self):
        provider = _FakeSummaryProvider()
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
            })
            history, meta, warnings, apply_limit = run(service._prepare_history(
                self.messages(17), {}, session_id="s1", generate=True
            ))
            self.assertEqual(len(history), 17)
            self.assertFalse(meta["generated_this_request"])
            self.assertFalse(apply_limit)
            self.assertFalse(warnings)
            self.assertFalse(provider.calls)

    def test_incremental_summary_updates_boundary_without_duplicates(self):
        provider = _FakeSummaryProvider()
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
                "summary_provider_id": "summary",
            })
            state = {}
            first = self.messages(18)
            history, meta, _, apply_limit = run(service._prepare_history(
                first, state, session_id="s1", generate=True
            ))
            self.assertEqual(len(history), 12)
            self.assertEqual(meta["covered_messages"], 6)
            self.assertFalse(apply_limit)
            second = first + self.messages(6)
            for index, message in enumerate(second[18:], start=18):
                message["content"] = f"message-{index}"
            history, meta, _, _ = run(service._prepare_history(
                second, state, session_id="s1", generate=True
            ))
            self.assertEqual(len(history), 12)
            self.assertEqual(meta["covered_messages"], 12)
            self.assertEqual(len(provider.calls), 2)
            self.assertIn("summary-1", provider.calls[1]["prompt"])

    def test_summary_failure_preserves_progress_and_uses_legacy_limit(self):
        provider = _FakeSummaryProvider(error=RuntimeError("down"))
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
            })
            state = {}
            history, meta, warnings, apply_limit = run(service._prepare_history(
                self.messages(18), state, session_id="s1", generate=True
            ))
            self.assertEqual(len(history), 18)
            self.assertTrue(apply_limit)
            self.assertNotIn("history_summary", state)
            self.assertIn("down", meta["error"])
            self.assertTrue(warnings)

    def test_configured_provider_missing_does_not_fallback(self):
        provider = _FakeSummaryProvider("current")
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
                "summary_provider_id": "missing",
            })
            _, meta, warnings, apply_limit = run(service._prepare_history(
                self.messages(18), {}, session_id="s1", generate=True
            ))
            self.assertFalse(provider.calls)
            self.assertIn("missing", meta["error"])
            self.assertTrue(warnings)
            self.assertTrue(apply_limit)

    def test_summary_timeout_degrades(self):
        provider = _SlowSummaryProvider()
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
                "summary_timeout_seconds": 1,
            })
            state = {}
            _, meta, warnings, apply_limit = run(service._prepare_history(
                self.messages(18), state, session_id="s1", generate=True
            ))
            self.assertTrue(apply_limit)
            self.assertTrue(warnings)
            self.assertFalse(meta["generated_this_request"])
            self.assertNotIn("history_summary", state)

    def test_simulation_never_calls_summary_provider_or_persists_state(self):
        provider = _FakeSummaryProvider()
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base", "summary": "static"})
            storage.bind("global", "*", "preset", preset_id)
            messages = self.messages(20)
            state = {
                "turn": 0, "effects": {}, "variables": {},
                "history_summary": {
                    "content": "existing", "covered_until": TavernService._message_fingerprint(messages[0]),
                    "covered_messages": 1, "updated_at": 100.0, "provider_id": "summary",
                },
            }
            storage.save_session("s1", state)
            service = TavernService(storage, _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
            })
            result = run(service.simulate({"session_id": "s1", "contexts": messages, "prompt": "now"}))
            self.assertFalse(provider.calls)
            self.assertTrue(result["summary"]["would_generate"])
            self.assertEqual(result["summary"]["content"], "existing")
            self.assertEqual(storage.get_session("s1"), state)


class _BoomEmbeddingProvider:
    provider_config = {"id": "emb"}

    async def get_embedding(self, text):
        raise RuntimeError("provider down")


class _ConcurrencyTracker:
    def __init__(self):
        self.entered = 0
        self.max_concurrent = 0
        self._lock = asyncio.Lock()

    async def enter(self):
        async with self._lock:
            self.entered += 1
            self.max_concurrent = max(self.max_concurrent, self.entered)

    async def exit(self):
        async with self._lock:
            self.entered -= 1


class _SlowOmniDraw:
    def __init__(self, tracker):
        self.tracker = tracker

    async def generate_images_for_plugin(self, **kwargs):
        await self.tracker.enter()
        await asyncio.sleep(0.05)
        await self.tracker.exit()
        return {"success": False, "message": "tracked"}


class EstimateTokensTests(unittest.TestCase):
    def test_english_four_chars_per_token(self):
        self.assertEqual(estimate_tokens("abcdefgh" * 10), 20)

    def test_chinese_lower_than_char_count(self):
        text = "中" * 160
        self.assertEqual(estimate_tokens(text), 100)
        self.assertLess(estimate_tokens(text), 160)

    def test_mixed_english_and_chinese(self):
        self.assertEqual(estimate_tokens("hello世界"), 2)

    def test_empty_returns_zero(self):
        self.assertEqual(estimate_tokens(""), 0)


class VectorMatcherTests(unittest.TestCase):
    def test_degrades_on_provider_error(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            ctx = _FakeEmbeddingContext([_BoomEmbeddingProvider()])
            service = TavernService(storage, ctx, {"vector_enabled": True, "embedding_provider_id": "emb"})
            entries = normalize_entries({"entries": [entry("v", ["k"], "hello", vectorized=True)]})
            self.assertEqual(run(service._vector_matcher("query", entries)), {})

    def test_caches_entry_embeddings_across_calls(self):
        provider = _FakeEmbeddingProvider("emb", {"query": [1.0, 0.0], "hello": [0.0, 1.0]})
        ctx = _FakeEmbeddingContext([provider])
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            service = TavernService(storage, ctx, {"vector_enabled": True, "embedding_provider_id": "emb"})
            entries = normalize_entries({"entries": [entry("v", ["k"], "hello", vectorized=True)]})
            run(service._vector_matcher("query", entries))
            run(service._vector_matcher("query", entries))
            self.assertEqual(provider.calls.count("hello"), 1)
            self.assertEqual(provider.calls.count("query"), 2)


class LongTermMemoryTests(unittest.TestCase):
    def test_memory_crud_persists_embedding_and_toggle_delete(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            memory_id = storage.put_memory(
                scope_type="session",
                scope_id="s1",
                category="plot",
                content="Gate opened",
                embedding=[1.0, 0.0],
            )
            items = storage.list_memories(scope_type="session", scope_id="s1")
            self.assertEqual(items[0]["embedding"], [1.0, 0.0])
            self.assertTrue(items[0]["enabled"])
            self.assertTrue(storage.set_memory_enabled(memory_id, False))
            self.assertFalse(storage.list_memories(scope_type="session", scope_id="s1")[0]["enabled"])
            self.assertTrue(storage.delete_memory(memory_id))
            self.assertEqual(storage.list_memories(scope_type="session", scope_id="s1"), [])

    def test_enabled_memory_is_retrieved_and_injected(self):
        provider = _FakeEmbeddingProvider("emb", {"tea": [1.0, 0.0], "User likes tea": [1.0, 0.0], "disabled": [1.0, 0.0]})
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="User likes tea", embedding=[1.0, 0.0],
            )
            storage.put_memory(
                scope_type="session", scope_id="s1", category="status",
                content="disabled", embedding=[1.0, 0.0], enabled=False,
            )
            service = TavernService(storage, _FakeEmbeddingContext([provider]), {
                "memory_enabled": True,
                "embedding_provider_id": "emb",
                "memory_top_k": 3,
            })
            result = run(service.simulate({"session_id": "s1", "prompt": "tea"}))
            memory_block = next(block for block in result["blocks"] if block["id"] == "memory")
            self.assertIn("User likes tea", memory_block["content"])
            self.assertNotIn("disabled", memory_block["content"])
            self.assertEqual(len(result["memory"]["matches"]), 1)
            self.assertEqual(storage.list_metrics(), [])

    def test_memory_extraction_failure_does_not_block_process(self):
        embedding = _FakeEmbeddingProvider("emb", {"hello": [1.0, 0.0]})
        memory_provider = _FakeMemoryProvider(error=RuntimeError("memory down"))
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            service = TavernService(storage, _FakeMemoryContext(embedding, memory_provider), {
                "memory_enabled": True,
                "memory_extract_interval": 1,
                "embedding_provider_id": "emb",
            })
            result = run(service.process(_MockEvent(), _MockReq()))
            self.assertEqual(result.system_prompt, "base")
            self.assertEqual(storage.list_memories(), [])
            self.assertEqual(len(storage.list_metrics()), 1)

    def test_real_process_records_metrics_but_simulation_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            service = TavernService(storage, object(), {})
            run(service.simulate({"session_id": "s1", "prompt": "hello"}))
            self.assertEqual(storage.list_metrics(), [])
            run(service.process(_MockEvent(), _MockReq()))
            metrics = storage.list_metrics()
            self.assertEqual(len(metrics), 1)
            self.assertEqual(metrics[0]["session_id"], "s1")
            self.assertGreaterEqual(metrics[0]["prompt_tokens"], 1)


class IllustrationConcurrencyTests(unittest.TestCase):
    def test_concurrency_limits_simultaneous_illustrations(self):
        tracker = _ConcurrencyTracker()
        bridge = OmniDrawBridge(_FakeContext(_FakeStar(_SlowOmniDraw(tracker))), {
            "illustration_enabled": True,
            "illustration_max_concurrency": 2,
            "illustration_mode": "text2img",
        })

        async def scenario():
            for _ in range(5):
                await bridge.maybe_illustrate(_FakeEvent(), _FakeResponse("a" * 100))
            await asyncio.gather(*bridge._tasks)

        run(scenario())
        self.assertLessEqual(tracker.max_concurrent, 2)
        self.assertGreaterEqual(tracker.max_concurrent, 1)


class _MockReq:
    def __init__(self):
        self.contexts = []
        self.prompt = "hello"
        self.system_prompt = ""
        self.session_id = "s1"
        self.conversation = None


class _MockEvent:
    def __init__(self, session_id="s1"):
        self.unified_msg_origin = session_id
        self._extras = {}

    def get_extra(self, key):
        return self._extras.get(key)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_name(self):
        return "TestUser"

    def get_sender_id(self):
        return "u1"

    def get_group_id(self):
        return ""


class SessionConcurrencyTests(unittest.TestCase):
    def test_process_persists_and_injects_generated_summary(self):
        provider = _FakeSummaryProvider()
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base", "summary": "static"})
            storage.bind("global", "*", "preset", preset_id)
            service = TavernService(storage, _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
            })
            req = _MockReq()
            req.contexts = SummaryCompressionTests.messages(18)
            result = run(service.process(_MockEvent(), req))
            state = storage.get_session("s1")
            self.assertEqual(state["history_summary"]["content"], "summary-1")
            summary = next(block for block in result.blocks if block.identifier == "summary")
            self.assertIn("static", summary.content)
            self.assertIn("summary-1", summary.content)
            preview = storage.get_preview("s1")
            self.assertTrue(preview["summary"]["generated_this_request"])
            self.assertNotIn("content", preview["summary"])

    def test_concurrent_process_does_not_lose_turns(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            service = TavernService(storage, object(), {})

            async def scenario():
                await asyncio.gather(
                    service.process(_MockEvent(), _MockReq()),
                    service.process(_MockEvent(), _MockReq()),
                )

            run(scenario())
            state = storage.get_session("s1")
            self.assertEqual(state["turn"], 2)

    def test_process_consumes_pending_generation(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            storage.save_session("s1", {"turn": 0, "effects": {}, "variables": {},
                                        "pending_generation": {"mode": "continue", "prompt": "extra"}})
            service = TavernService(storage, object(), {})
            result = run(service.process(_MockEvent(), _MockReq()))
            state = storage.get_session("s1")
            self.assertNotIn("pending_generation", state)
            self.assertTrue(any(block.identifier == "continue" for block in result.blocks))

    def test_pending_generation_and_reset_share_session_lock(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            storage.save_session("s1", {"turn": 7, "effects": {}, "variables": {}})
            service = TavernService(storage, object(), {})

            async def scenario():
                async with service._session_lock("s1"):
                    pending_task = asyncio.create_task(
                        service.set_pending_generation("s1", "quiet", "extra")
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(pending_task.done())
                await pending_task
                self.assertEqual(storage.get_session("s1")["turn"], 7)
                self.assertEqual(
                    storage.get_session("s1")["pending_generation"],
                    {"mode": "quiet", "prompt": "extra"},
                )

                async with service._session_lock("s1"):
                    reset_task = asyncio.create_task(service.reset_session("s1"))
                    await asyncio.sleep(0)
                    self.assertFalse(reset_task.done())
                await reset_task

            run(scenario())
            self.assertEqual(storage.get_session("s1")["turn"], 0)


if __name__ == "__main__":
    unittest.main()
