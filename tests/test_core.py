import asyncio
import copy
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

from aiohttp import ClientSession, web
from astrbot_plugin_komeiji_tavern.constants import API_PREFIX, PLUGIN_ID, PLUGIN_VERSION
from astrbot_plugin_komeiji_tavern.cipher import CIPHER_TAG, CipherCodec
from astrbot_plugin_komeiji_tavern.importers import detect_kind, export_document, parse_binary_payload, parse_payload, preview_import
from astrbot_plugin_komeiji_tavern.documents import validate_document
from astrbot_plugin_komeiji_tavern.lore import LoreScanner, normalize_entries
from astrbot_plugin_komeiji_tavern.models import BuildResult, Position, ScanResult
from astrbot_plugin_komeiji_tavern.prompt_builder import PromptBuilder, estimate_tokens
from astrbot_plugin_komeiji_tavern.qq_delivery import split_forward_text
from astrbot_plugin_komeiji_tavern.storage import TavernStorage
from astrbot_plugin_komeiji_tavern.service import TavernService
from astrbot_plugin_komeiji_tavern.url_request import (
    FETCH_TOOL_NAME,
    SUBMIT_REPLY_TOOL_NAME,
    URLRequestBroker,
    URLRequestProtocolError,
    request_protocol_prompt,
    strip_reasoning_tags,
)
from astrbot_plugin_komeiji_tavern.web import TavernWebApi
from astrbot_plugin_komeiji_tavern.main import KomeijiTavernPlugin, _flatten_config, _latest_plain_turn, _remove_last_completed_turn
from astrbot_plugin_komeiji_tavern.illustration import OmniDrawBridge
from astrbot_plugin_komeiji_tavern.export_utils import (
    build_document_archive,
    build_session_backup,
    document_download,
    safe_filename,
)
from astrbot.api.message_components import At, Nodes, Plain
from astrbot.api.provider import LLMResponse
from astrbot.core.agent.message import ImageURLPart, Message, TextPart
from astrbot.core.message.message_event_result import MessageEventResult


def run(coro):
    return asyncio.run(coro)


def entry(uid, keys, content, **extra):
    return {"uid": uid, "key": keys, "content": content, **extra}


class CampaignStateTests(unittest.TestCase):
    def test_model_reasoning_tags_are_removed_from_visible_reply(self):
        self.assertEqual(
            strip_reasoning_tags(
                "<thought>private chain</thought>\nVisible answer."
            ),
            "Visible answer.",
        )
        self.assertEqual(
            strip_reasoning_tags(
                "Prefix\n<THINK stage=\"internal\">secret</THINK>\nSuffix"
            ),
            "Prefix\n\nSuffix",
        )
        self.assertEqual(
            strip_reasoning_tags("<analysis>truncated private chain"),
            "",
        )

    def test_llm_response_strips_reasoning_before_downstream_processing(self):
        class Event:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {
            "status_bar_enabled": False,
            "illustration_enabled": False,
        }
        response = LLMResponse(
            role="assistant",
            completion_text="<thought>private chain</thought>\nVisible answer.",
        )

        run(plugin.on_llm_response(Event(), response))

        self.assertEqual(response.completion_text, "Visible answer.")

    def test_url_request_protocol_allows_fetch_then_submit_reply(self):
        prompt = request_protocol_prompt(
            fetch_tool_enabled=True,
            reply_tool_enabled=True,
        )
        self.assertIn("Do not call fetch_request_url again.", prompt)
        self.assertIn("fetch_request_url (at most once) and submit_reply (exactly once)", prompt)
        self.assertNotIn("Do not call any other tool.", prompt)
        self.assertIn("Do not send a progress update", prompt)
        self.assertIn("immediately", prompt)

    def test_url_request_progress_replies_are_not_accepted_as_fallbacks(self):
        progress_replies = (
            "小提示：当前已解析出授权请求，正在构建最终回复。请稍候！",
            "Please wait while I prepare the final reply.",
        )
        for reply in progress_replies:
            with self.subTest(reply=reply):
                self.assertTrue(
                    KomeijiTavernPlugin._is_url_request_progress_reply(reply)
                )
        self.assertFalse(
            KomeijiTavernPlugin._is_url_request_progress_reply("Here is the final answer.")
        )

    def test_auxiliary_llm_uses_astrbot_fallback_order(self):
        calls = []

        class Provider:
            def __init__(self, provider_id, error=None):
                self.provider_config = {"id": provider_id}
                self.error = error

            async def text_chat(self, **kwargs):
                calls.append((self.provider_config["id"], kwargs["prompt"]))
                if self.error:
                    raise RuntimeError(self.error)
                return type("Response", (), {"role": "assistant", "completion_text": "ok"})()

        providers = {
            "primary": Provider("primary", "expired"),
            "fallback-1": Provider("fallback-1", "busy"),
            "fallback-2": Provider("fallback-2"),
        }

        class Context:
            @staticmethod
            def get_config(umo=None):
                self.assertEqual(umo, "session-1")
                return {"provider_settings": {
                    "fallback_chat_models": ["primary", "fallback-1", "fallback-2"]
                }}

            @staticmethod
            def get_provider_by_id(provider_id):
                return providers.get(provider_id)

        service = TavernService(object(), Context(), {})
        response, provider_id = run(service._text_chat_with_fallback(
            session_id="session-1", provider=providers["primary"], prompt="state",
        ))
        self.assertEqual(response.completion_text, "ok")
        self.assertEqual(provider_id, "fallback-2")
        self.assertEqual(calls, [
            ("primary", "state"), ("fallback-1", "state"), ("fallback-2", "state"),
        ])

    def test_astrbot_reset_or_new_starts_a_new_story_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            node_id = storage.create_story_node({
                "session_id": "s1", "assistant_text": "The old scene ends here.",
            })
            service = TavernService(storage, object(), {})
            state = {"current_story_node_id": node_id, "astrbot_conversation_id": "conv-old"}
            self.assertFalse(run(service._conversation_boundary_changed(
                state=state,
                messages=[{"role": "assistant", "content": "The old scene ends here."}],
                conversation_id="conv-old",
            )))
            self.assertTrue(run(service._conversation_boundary_changed(
                state=state, messages=[], conversation_id="conv-old",
            )))
            self.assertTrue(run(service._conversation_boundary_changed(
                state=state,
                messages=[{"role": "assistant", "content": "The old scene ends here."}],
                conversation_id="conv-new",
            )))
            fresh = service._fresh_conversation_state("conv-new")
            self.assertEqual(fresh["turn"], 0)
            self.assertTrue(fresh["archive_new_root"])
            self.assertNotIn("current_story_node_id", fresh)

    def test_story_summaries_expose_history_fingerprints_for_legacy_reset_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            parent_id = storage.create_story_node({
                "session_id": "s1", "assistant_text": "Previous reply",
            })
            storage.create_story_node({
                "session_id": "s1", "parent_id": parent_id,
                "request_messages": [{"role": "assistant", "content": "Previous reply"}],
                "assistant_text": "Current reply",
            })
            nodes = storage.list_story_nodes("s1")
            parent = next(item for item in nodes if item["id"] == parent_id)
            child = next(item for item in nodes if item["parent_id"] == parent_id)
            self.assertIn(parent["assistant_fingerprint"], child["history_assistant_fingerprints"])
            self.assertTrue(child["parent_history_continuous"])

            disconnected_id = storage.create_story_node({
                "session_id": "s1", "parent_id": child["id"],
                "request_messages": [{"role": "user", "content": "new topic"}],
                "assistant_text": "A fresh conversation",
            })
            disconnected = next(item for item in storage.list_story_nodes("s1") if item["id"] == disconnected_id)
            self.assertFalse(disconnected["parent_history_continuous"])

    def test_campaign_state_is_injected_even_when_preset_has_no_memory_block(self):
        builder = PromptBuilder()
        result = builder.build(
            original_system="", contexts=[], current_prompt="continue",
            preset={"main_prompt": "base", "blocks": [{"identifier": "main", "name": "Main"}]},
            character=None, persona="", lore=ScanResult(), values={},
            campaign_context="[当前权威状态]\n{\"ammo\": 4}",
        )
        campaign_block = next(block for block in result.blocks if block.identifier == "campaign")
        self.assertIn('"ammo": 4', campaign_block.content)
        self.assertIn('"ammo": 4', result.messages[0]["content"])

    def test_campaign_block_is_suppressed_without_campaign_context(self):
        builder = PromptBuilder()
        result = builder.build(
            original_system="", contexts=[], current_prompt="continue",
            preset={"blocks": [
                {"identifier": "main", "name": "Main", "content": "base"},
                {"identifier": "campaign", "name": "Campaign State", "content": "[当前权威状态]\n{\"ammo\": 4}"},
            ]},
            character=None, persona="", lore=ScanResult(), values={},
            campaign_context="",
        )
        self.assertFalse(any(block.identifier == "campaign" for block in result.blocks))
        self.assertNotIn('"ammo": 4', json.dumps(result.messages, ensure_ascii=False))

    def test_campaign_can_span_sessions_and_resolve_world_ruleset_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            campaign_id = storage.save_campaign({
                "name": "Rain City", "world_id": "rain-world", "ruleset_id": "survival",
                "state_data": {"inventory": [{"name": "water", "count": 2}]},
            })
            self.assertTrue(storage.bind_campaign_session(campaign_id, "session-a"))
            self.assertTrue(storage.bind_campaign_session(campaign_id, "session-b"))
            self.assertEqual(storage.campaign_for_session("session-b")["id"], campaign_id)

            class Event:
                unified_msg_origin = "session-b"
                def get_sender_id(self): return "user-1"
                def get_group_id(self): return "group-1"

            scopes = TavernService(storage, object(), {}).scopes(Event(), None)
            self.assertIn(("campaign", campaign_id), scopes)
            self.assertIn(("world", "rain-world"), scopes)
            self.assertIn(("ruleset", "survival"), scopes)

            class PlainEvent:
                unified_msg_origin = "session-plain"
                def get_sender_id(self): return "user-1"
                def get_group_id(self): return "group-1"

            plain_scopes = TavernService(storage, object(), {}).scopes(PlainEvent(), None)
            self.assertNotIn(("campaign", campaign_id), plain_scopes)
            self.assertNotIn(("world", "rain-world"), plain_scopes)
            self.assertNotIn(("ruleset", "survival"), plain_scopes)

    def test_campaign_state_patch_is_deterministic_and_auditable(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            campaign_id = storage.save_campaign({"name": "Test", "state_data": {"ammo": 5, "clues": []}})
            before = storage.get_campaign(campaign_id)["state_data"]
            patch_data = [
                {"op": "increment", "path": "ammo", "value": -1},
                {"op": "append", "path": "clues", "value": "red door"},
            ]
            after = TavernService._apply_state_patch(before, patch_data)
            self.assertEqual(after, {"ammo": 4, "clues": ["red door"]})
            change_id = storage.put_campaign_state_change({
                "campaign_id": campaign_id, "story_node_id": "node-1",
                "patch": patch_data, "before_state": before, "after_state": after,
            })
            self.assertEqual(storage.list_campaign_state_changes(campaign_id)[0]["status"], "pending")
            storage.set_campaign_state_change_status(change_id, "applied", before_state=before, after_state=after)
            self.assertEqual(storage.applied_campaign_state_for_story_node("node-1"), after)

    def test_rp_pack_starts_new_game_and_cleans_session_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            preset_id = storage.put_document("preset", "Preset", {"main_prompt": "base"})
            lore_id = storage.put_document("lorebook", "World", {"entries": []})
            old_campaign_id = storage.save_campaign({"name": "Old", "state_data": {"points": 12}})
            storage.bind_campaign_session(old_campaign_id, "session-1")
            storage.bind("campaign", old_campaign_id, "preset", preset_id, 0)
            storage.bind("campaign", old_campaign_id, "lorebook", lore_id, 0)
            storage.bind("session", "session-1", "preset", "stale-session-preset", 0)
            pack = TavernService(storage, object(), {}).create_pack_from_campaign(old_campaign_id, "Survival Pack")

            class ConversationManager:
                def __init__(self):
                    self.created = []
                async def new_conversation(self, session_id, platform_id=None):
                    self.created.append((session_id, platform_id))
                    return "conv-new"

            class Context:
                conversation_manager = ConversationManager()

            result = run(TavernService(storage, Context(), {}).start_new_game(
                pack_id=pack["id"], session_id="session-1", name="Fresh Run",
            ))
            new_campaign = result["campaign"]
            self.assertEqual(result["conversation_id"], "conv-new")
            self.assertEqual(storage.get_campaign(old_campaign_id)["archived"], True)
            self.assertEqual(storage.campaign_for_session("session-1")["id"], new_campaign["id"])
            self.assertEqual(new_campaign["name"], "Fresh Run")
            self.assertEqual(new_campaign["state_data"], {"points": 12})
            self.assertEqual(storage.get_session("session-1")["turn"], 0)
            self.assertFalse(storage.list_bindings(scope_type="session", scope_id="session-1"))
            copied = storage.list_bindings(scope_type="campaign", scope_id=new_campaign["id"])
            self.assertEqual({item["kind"]: item["target_id"] for item in copied}, {
                "preset": preset_id,
                "lorebook": lore_id,
            })
            self.assertEqual(Context.conversation_manager.created, [("session-1", None)])

    def test_web_play_turn_generates_and_archives_story_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            preset_id = storage.put_document("preset", "Preset", {"main_prompt": "{{original_system}}"})
            storage.bind("global", "*", "preset", preset_id, 0)
            campaign_id = storage.save_campaign({"name": "Web Run", "state_data": {"location": "门厅"}})
            storage.bind_campaign_session(campaign_id, "session-web")

            class Provider:
                provider_config = {"id": "web-provider"}
                def __init__(self):
                    self.calls = []
                async def text_chat(self, **kwargs):
                    self.calls.append(kwargs)
                    return type("Response", (), {"completion_text": "门轴轻响，房间里有人屏住了呼吸。"})()

            class Context:
                def __init__(self, provider):
                    self.provider = provider
                def get_using_provider(self, _session_id):
                    return self.provider
                def get_config(self, umo=None):
                    return {}
                def get_provider_by_id(self, _provider_id):
                    return None

            provider = Provider()
            service = TavernService(storage, Context(provider), {"memory_enabled": False})
            result = run(service.play_web_turn(session_id="session-web", prompt="我推开门。"))
            self.assertEqual(result["provider_id"], "web-provider")
            self.assertIn("门轴轻响", result["reply"])
            self.assertTrue(result["node_id"])
            node = storage.get_story_node(result["node_id"])
            self.assertEqual(node["session_id"], "session-web")
            self.assertIn("我推开门", json.dumps(node["request_messages"], ensure_ascii=False))
            self.assertEqual(storage.get_session("session-web")["current_story_node_id"], result["node_id"])
            self.assertTrue(provider.calls)
            self.assertNotIn("messages", provider.calls[0])
            self.assertIn("prompt", provider.calls[0])
            self.assertIn("我推开门", provider.calls[0]["prompt"])

    def test_web_play_turn_sends_ciphertext_but_archives_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = TavernStorage(Path(tmp) / "tavern.db")
            preset_id = storage.put_document("preset", "Preset", {"main_prompt": "secret system"})
            storage.bind("global", "*", "preset", preset_id, 0)
            codec = CipherCodec("base64_utf8")

            class Provider:
                provider_config = {"id": "cipher-provider"}

                def __init__(self):
                    self.calls = []

                async def text_chat(self, **kwargs):
                    self.calls.append(kwargs)
                    return type("Response", (), {
                        "role": "assistant",
                        "completion_text": codec.encode_content("解码后的网页回复"),
                    })()

            class Context:
                def __init__(self, provider):
                    self.provider = provider

                def get_using_provider(self, _session_id):
                    return self.provider

                def get_config(self, umo=None):
                    return {}

                def get_provider_by_id(self, _provider_id):
                    return None

            provider = Provider()
            service = TavernService(storage, Context(provider), {
                "cipher_enabled": True,
                "cipher_method": "base64_utf8",
                "memory_enabled": False,
            })
            result = run(service.play_web_turn(
                session_id="session-cipher",
                prompt="网页明文行动",
            ))
            self.assertEqual(result["reply"], "解码后的网页回复")
            self.assertTrue(provider.calls)
            self.assertIn(f"<{CIPHER_TAG}>", provider.calls[0]["prompt"])
            self.assertNotIn("网页明文行动", provider.calls[0]["prompt"])
            self.assertIn("Encoding method: base64_utf8", provider.calls[0]["system_prompt"])

            preview = storage.get_preview("session-cipher")
            self.assertIn("网页明文行动", json.dumps(preview["messages"], ensure_ascii=False))
            self.assertTrue(preview["cipher"]["provider_request_encoded"])
            node = storage.get_story_node(result["node_id"])
            self.assertIn("网页明文行动", json.dumps(node["request_messages"], ensure_ascii=False))
            self.assertEqual(node["assistant_text"], "解码后的网页回复")

    def test_web_chat_completion_uses_astrbot_prompt_contexts_shape(self):
        calls = []

        class Provider:
            provider_config = {"id": "legacy-provider"}
            async def text_chat(self, **kwargs):
                calls.append(kwargs)
                if "messages" in kwargs:
                    raise TypeError("messages is not supported")
                return type("Response", (), {"completion_text": "ok"})()

        class Context:
            @staticmethod
            def get_using_provider(_session_id):
                return Provider()
            @staticmethod
            def get_config(umo=None):
                return {}
            @staticmethod
            def get_provider_by_id(_provider_id):
                return None

        service = TavernService(object(), Context(), {})
        response, provider_id = run(service._chat_completion_with_fallback(
            session_id="session-web",
            messages=[
                {"role": "system", "content": "rules"},
                {"role": "assistant", "content": "previous"},
                {"role": "user", "content": "continue"},
            ],
        ))
        self.assertEqual(response.completion_text, "ok")
        self.assertEqual(provider_id, "legacy-provider")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("messages", calls[0])
        self.assertEqual(calls[0]["prompt"], "continue")
        self.assertEqual(calls[0]["contexts"], [{"role": "assistant", "content": "previous"}])
        self.assertEqual(calls[0]["system_prompt"], "rules")


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
            self.assertEqual(json.loads(archive.read("story-nodes.json")), [])

    def test_session_backup_can_include_story_nodes(self):
        nodes = [{"id": "n1", "assistant_text": "reply"}]
        with self.read_zip(build_session_backup("s1", {}, {}, nodes)) as archive:
            self.assertEqual(json.loads(archive.read("story-nodes.json")), nodes)
            self.assertEqual(json.loads(archive.read("manifest.json"))["story_node_count"], 1)


class CoreTests(unittest.TestCase):
    def test_url_request_broker_serves_html_json_head_and_deletes(self):
        async def scenario():
            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example/komeiji",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
                plugin_version="test",
            )
            await broker.start()
            self.assertTrue(broker.running)
            handle = await broker.create([
                {"role": "system", "content": "规则 <不得泄漏>"},
                {"role": "user", "content": "中文 😀\n  保留空白"},
            ])
            local_url = f"{broker.local_base_url}/request/{handle.token}"
            try:
                async with ClientSession() as client:
                    async with client.get(local_url) as response:
                        html_text = await response.text()
                        self.assertEqual(response.status, 200)
                        self.assertIn("&lt;不得泄漏&gt;", html_text)
                        self.assertIn("中文 😀\n  保留空白", html_text)
                        self.assertEqual(
                            response.headers["Cache-Control"],
                            "no-store, no-cache, must-revalidate, max-age=0",
                        )
                        self.assertIn("noindex", response.headers["X-Robots-Tag"])
                        self.assertIn("default-src 'none'", response.headers["Content-Security-Policy"])
                    async with client.get(
                        local_url,
                        headers={"Accept": "application/json"},
                    ) as response:
                        payload = await response.json()
                        self.assertEqual(payload["messages"][0]["role"], "system")
                        self.assertEqual(
                            payload["messages"][1]["content"],
                            "中文 😀\n  保留空白",
                        )
                    async with client.head(local_url) as response:
                        self.assertEqual(response.status, 200)
                    async with client.get(
                        f"{broker.local_base_url}/request/not-a-token"
                    ) as response:
                        self.assertEqual(response.status, 404)
                    await broker.delete(handle)
                    async with client.get(local_url) as response:
                        self.assertEqual(response.status, 404)
            finally:
                await broker.stop()

        run(scenario())

    def test_url_request_tool_is_bound_to_exact_url_and_once_per_consumer(self):
        async def scenario():
            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
            )
            await broker.start()
            try:
                handle = await broker.create([
                    {"role": "user", "content": "only this request"},
                ])
                content = broker.tool_content(
                    handle,
                    handle.url,
                    consumer_id="provider-a",
                )
                self.assertIn("only this request", content)
                with self.assertRaises(ValueError):
                    broker.tool_content(
                        handle,
                        handle.url,
                        consumer_id="provider-a",
                    )
                with self.assertRaises(ValueError):
                    broker.tool_content(
                        handle,
                        "https://attacker.example/request/other",
                        consumer_id="provider-b",
                    )
                self.assertIn(
                    "only this request",
                    broker.tool_content(
                        handle,
                        handle.url,
                        consumer_id="provider-b",
                    ),
                )
                submitted = await broker.submit_reply(
                    handle,
                    "<thought>private chain</thought>\nwebsite reply",
                    consumer_id="provider-a",
                )
                self.assertEqual(submitted, "website reply")
                entry = broker._entry_for_handle(handle)
                self.assertIsNotNone(entry)
                self.assertEqual(
                    entry.messages[-1],
                    {"role": "assistant", "content": "website reply"},
                )
                with self.assertRaises(ValueError):
                    await broker.submit_reply(
                        handle,
                        "duplicate reply",
                        consumer_id="provider-a",
                    )
            finally:
                await broker.stop()

        run(scenario())

    def test_url_request_start_failure_and_ttl_cleanup_are_explicit(self):
        async def scenario():
            invalid = URLRequestBroker(
                enabled=True,
                public_base_url="",
                listen_host="127.0.0.1",
                listen_port=0,
            )
            await invalid.start()
            self.assertFalse(invalid.running)
            self.assertTrue(invalid.start_error)
            with self.assertRaises(RuntimeError):
                await invalid.create(
                    [{"role": "user", "content": "must not leak"}]
                )

            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
            )
            await broker.start()
            try:
                occupied = URLRequestBroker(
                    enabled=True,
                    public_base_url="https://public.example",
                    listen_host="127.0.0.1",
                    listen_port=broker.bound_port,
                )
                await occupied.start()
                self.assertFalse(occupied.running)
                self.assertTrue(occupied.start_error)
                handle = await broker.create([{"role": "user", "content": "expires"}])
                broker._entries[handle.token].expires_at = 0
                self.assertEqual(await broker.cleanup_expired(), 1)
                self.assertFalse(broker.exists(handle))
            finally:
                await broker.stop()

        run(scenario())

    def test_hosted_url_request_backend_uses_authenticated_crud_and_local_tool_cache(self):
        async def scenario():
            api_key = "test-hosted-key"
            records = {}
            calls = []
            create_attempts = 0

            async def authorized(request):
                calls.append((request.method, request.path))
                if request.headers.get("Authorization") != f"Bearer {api_key}":
                    return web.json_response({"error": "unauthorized"}, status=401)
                return None

            async def status(request):
                denied = await authorized(request)
                if denied:
                    return denied
                return web.json_response({"status": "ok"})

            async def create(request):
                nonlocal create_attempts
                denied = await authorized(request)
                if denied:
                    return denied
                create_attempts += 1
                if create_attempts == 1:
                    return web.Response(status=403)
                body = await request.json()
                self.assertEqual(body["content_encoding"], "base64_utf8")
                records["request-1"] = [
                    {
                        "role": item["role"],
                        "content": base64.b64decode(
                            item["content_base64"]
                        ).decode("utf-8"),
                    }
                    for item in body["messages"]
                ]
                origin = f"{request.scheme}://{request.host}"
                return web.json_response({
                    "request_id": "request-1",
                    "read_url": f"{origin}/request/read-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                }, status=201)

            async def update(request):
                denied = await authorized(request)
                if denied:
                    return denied
                body = await request.json()
                self.assertEqual(body["content_encoding"], "base64_utf8")
                records[request.match_info["request_id"]] = [
                    {
                        "role": item["role"],
                        "content": base64.b64decode(
                            item["content_base64"]
                        ).decode("utf-8"),
                    }
                    for item in body["messages"]
                ]
                return web.json_response({"status": "updated"})

            async def delete(request):
                denied = await authorized(request)
                if denied:
                    return denied
                records.pop(request.match_info["request_id"], None)
                return web.Response(status=204)

            app = web.Application()
            app.router.add_get("/api/v1/status", status)
            app.router.add_post("/api/v1/requests", create)
            app.router.add_put("/api/v1/requests/{request_id}", update)
            app.router.add_delete("/api/v1/requests/{request_id}", delete)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            broker = URLRequestBroker(
                enabled=True,
                backend="hosted",
                public_base_url="",
                hosted_api_base_url=f"http://127.0.0.1:{port}",
                hosted_api_key=api_key,
                hosted_timeout_seconds=5,
                hosted_proxy_url="",
                ttl_seconds=60,
            )
            try:
                await broker.start()
                self.assertTrue(broker.running)
                handle = await broker.create([
                    {"role": "user", "content": "first plaintext"},
                ])
                self.assertEqual(handle.request_id, "request-1")
                self.assertNotIn(api_key, handle.url)
                await broker.update(handle, [
                    {"role": "user", "content": "updated plaintext"},
                ])
                self.assertEqual(
                    records["request-1"][0]["content"],
                    "updated plaintext",
                )
                tool_content = broker.tool_content(
                    handle,
                    handle.url,
                    consumer_id="hosted-model",
                )
                self.assertIn("updated plaintext", tool_content)
                self.assertTrue(await broker.delete(handle))
                self.assertFalse(broker.exists(handle))
                self.assertEqual(records, {})
                self.assertEqual(
                    calls,
                    [
                        ("GET", "/api/v1/status"),
                        ("POST", "/api/v1/requests"),
                        ("POST", "/api/v1/requests"),
                        ("PUT", "/api/v1/requests/request-1"),
                        ("DELETE", "/api/v1/requests/request-1"),
                    ],
                )
            finally:
                await broker.stop()
                await runner.cleanup()

        run(scenario())

    def test_url_request_mode_priority_suppresses_cipher(self):
        service = TavernService(object(), object(), {
            "url_request_enabled": True,
            "url_request_public_base_url": "https://public.example",
            "cipher_enabled": True,
            "cipher_method": "hex_utf8",
        })
        self.assertTrue(service.url_request_enabled)
        self.assertFalse(service.cipher_enabled)
        self.assertTrue(
            service.cipher_metadata(provider_encoded=False)[
                "suppressed_by_url_request"
            ]
        )
        self.assertTrue(
            service.url_request_metadata(provider_externalized=True)[
                "cipher_suppressed"
            ]
        )

    def test_url_request_agent_lifecycle_externalizes_and_restores_plaintext(self):
        logical = BuildResult(
            system_prompt="plain system",
            contexts=[{"role": "assistant", "content": "plain history"}],
            blocks=[],
            dropped=[],
            warnings=[],
            messages=[
                {"role": "system", "content": "plain system"},
                {"role": "assistant", "content": "plain history"},
                {"role": "user", "content": "plain prompt"},
            ],
            current_prompt="plain prompt",
        )

        async def scenario():
            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
            )
            await broker.start()

            class Service:
                url_request_enabled = True
                cipher_enabled = False
                url_requests = broker

                async def process(self, _event, _req):
                    return logical

            class Event:
                unified_msg_origin = "s-url"

                def __init__(self):
                    self.extras = {}
                    self.result = None
                    self.stopped = False

                def set_extra(self, key, value):
                    self.extras[key] = value

                def get_extra(self, key, default=None):
                    return self.extras.get(key, default)

                def set_result(self, result):
                    self.result = result

                def stop_event(self):
                    self.stopped = True

            plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
            plugin.context = object()
            plugin.config = {"tool_delivery_enabled": True}
            plugin.service = Service()
            event = Event()
            req = _MockReq()
            req.func_tool = object()

            try:
                await plugin.on_llm_request(event, req)
                handle = event.get_extra("_kt_url_request_handle")
                self.assertIsNotNone(handle)
                self.assertEqual(
                    [tool.name for tool in req.func_tool.tools],
                    [FETCH_TOOL_NAME, SUBMIT_REPLY_TOOL_NAME],
                )
                tool = req.func_tool.tools[0]
                tool_payload = await tool.handler(event, handle.url)
                self.assertIn("plain prompt", tool_payload)

                image = ImageURLPart(
                    image_url=ImageURLPart.ImageURL(
                        url="https://example.com/image.png"
                    )
                )

                class RunContext:
                    messages = [
                        Message(role="system", content="plain system"),
                        Message(role="assistant", content="plain history"),
                        Message(
                            role="user",
                            content=[TextPart(text="plain prompt"), image],
                        ),
                    ]

                run_context = RunContext()
                await plugin.on_agent_begin(event, run_context)
                provider_dump = json.dumps(
                    [message.model_dump() for message in run_context.messages],
                    ensure_ascii=False,
                )
                self.assertNotIn("plain system", provider_dump)
                self.assertNotIn("plain history", provider_dump)
                self.assertNotIn("plain prompt", provider_dump)
                self.assertIn(handle.url, provider_dump)
                self.assertIn("image.png", provider_dump)
                self.assertEqual(
                    [message.role for message in run_context.messages],
                    ["system", "user"],
                )

                submit_tool = req.func_tool.tools[1]
                acknowledgement = await submit_tool.handler(
                    event,
                    "plain answer",
                )
                self.assertIn("Reply accepted", acknowledgement)
                response = LLMResponse(
                    role="assistant",
                    completion_text="OK",
                )
                await plugin.on_llm_response(event, response)
                self.assertEqual(response.completion_text, "plain answer")
                await plugin.on_agent_done(event, run_context, response)
                persisted_dump = json.dumps(
                    [message.model_dump() for message in run_context.messages],
                    ensure_ascii=False,
                )
                self.assertIn("plain system", persisted_dump)
                self.assertIn("plain history", persisted_dump)
                self.assertIn("plain prompt", persisted_dump)
                self.assertIn("plain answer", persisted_dump)
                self.assertNotIn(handle.url, persisted_dump)
                self.assertFalse(broker.exists(handle))
            finally:
                await broker.stop()

        run(scenario())

    def test_url_request_web_driver_supports_native_and_tool_paths(self):
        async def scenario(use_tool: bool):
            with tempfile.TemporaryDirectory() as tmp:
                storage = TavernStorage(Path(tmp) / "tavern.db")

                class Provider:
                    provider_config = {"id": "url-provider"}

                    def __init__(self):
                        self.calls = []

                    async def text_chat(self, **kwargs):
                        self.calls.append(kwargs)
                        if use_tool and len(self.calls) == 1:
                            return LLMResponse(
                                role="assistant",
                                completion_text="",
                                tools_call_name=[FETCH_TOOL_NAME],
                                tools_call_args=[{"url": kwargs["prompt"].splitlines()[-1]}],
                                tools_call_ids=["call-1"],
                            )
                        return LLMResponse(
                            role="assistant",
                            completion_text="网页最终回复",
                        )

                class Context:
                    def __init__(self, provider):
                        self.provider = provider

                    def get_using_provider(self, _session_id):
                        return self.provider

                    def get_provider_by_id(self, _provider_id):
                        return None

                    def get_config(self, umo=None):
                        return {"provider_settings": {"fallback_chat_models": []}}

                provider = Provider()
                service = TavernService(storage, Context(provider), {
                    "url_request_enabled": True,
                    "url_request_public_base_url": "https://public.example",
                    "url_request_listen_host": "127.0.0.1",
                    "url_request_listen_port": 0,
                    "url_request_fetch_tool_enabled": True,
                    "memory_enabled": False,
                })
                await service.url_requests.start()
                try:
                    result = await service.play_web_turn(
                        session_id=f"url-web-{use_tool}",
                        prompt="网页明文行动",
                    )
                    self.assertEqual(result["reply"], "网页最终回复")
                    self.assertEqual(len(provider.calls), 2 if use_tool else 1)
                    first = provider.calls[0]
                    self.assertNotIn("网页明文行动", json.dumps(first, ensure_ascii=False, default=str))
                    self.assertIn("/request/", first["prompt"])
                    self.assertEqual(
                        [tool.name for tool in first["func_tool"].tools],
                        [FETCH_TOOL_NAME, SUBMIT_REPLY_TOOL_NAME],
                    )
                    if use_tool:
                        second_dump = json.dumps(
                            provider.calls[1]["contexts"],
                            ensure_ascii=False,
                        )
                        self.assertIn("网页明文行动", second_dump)
                        self.assertEqual(
                            provider.calls[1]["system_prompt"],
                            first["system_prompt"],
                        )
                    preview = storage.get_preview(f"url-web-{use_tool}")
                    preview_dump = json.dumps(preview, ensure_ascii=False)
                    self.assertIn("网页明文行动", preview_dump)
                    self.assertNotIn("/request/", preview_dump)
                    self.assertTrue(
                        preview["url_request"]["provider_request_externalized"]
                    )
                    self.assertEqual(service.url_requests._entries, {})
                finally:
                    await service.url_requests.stop()

        run(scenario(False))
        run(scenario(True))

    def test_url_request_web_driver_writes_submitted_reply_to_page(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                storage = TavernStorage(Path(tmp) / "tavern.db")

                class Provider:
                    provider_config = {"id": "submit-provider"}

                    def __init__(self):
                        self.calls = []

                    async def text_chat(self, **kwargs):
                        self.calls.append(kwargs)
                        return LLMResponse(
                            role="assistant",
                            completion_text="",
                            tools_call_name=[SUBMIT_REPLY_TOOL_NAME],
                            tools_call_args=[{"text": "submitted website reply"}],
                            tools_call_ids=["submit-1"],
                        )

                class Context:
                    def __init__(self, provider):
                        self.provider = provider

                    def get_using_provider(self, _session_id):
                        return self.provider

                    def get_provider_by_id(self, _provider_id):
                        return None

                    def get_config(self, umo=None):
                        return {"provider_settings": {"fallback_chat_models": []}}

                provider = Provider()
                service = TavernService(storage, Context(provider), {
                    "url_request_enabled": True,
                    "url_request_public_base_url": "https://public.example",
                    "url_request_listen_host": "127.0.0.1",
                    "url_request_listen_port": 0,
                    "url_request_fetch_tool_enabled": True,
                    "url_request_reply_tool_enabled": True,
                    "memory_enabled": False,
                })
                await service.url_requests.start()
                written = []
                original_submit = service.url_requests.submit_reply

                async def capture_submit(handle, text, *, consumer_id):
                    result = await original_submit(
                        handle,
                        text,
                        consumer_id=consumer_id,
                    )
                    entry = service.url_requests._entry_for_handle(handle)
                    written.append(copy.deepcopy(entry.messages))
                    return result

                service.url_requests.submit_reply = capture_submit
                try:
                    result = await service.play_web_turn(
                        session_id="url-web-submit",
                        prompt="write through the reply tool",
                    )
                    self.assertEqual(result["reply"], "submitted website reply")
                    self.assertEqual(len(provider.calls), 1)
                    self.assertEqual(
                        written[-1][-1],
                        {
                            "role": "assistant",
                            "content": "submitted website reply",
                        },
                    )
                    self.assertEqual(service.url_requests._entries, {})
                finally:
                    await service.url_requests.stop()

        run(scenario())

    def test_url_request_web_driver_rejects_wrong_or_repeated_tools(self):
        async def scenario(kind: str):
            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
            )
            await broker.start()
            handle = await broker.create([{"role": "user", "content": "secret"}])

            class Provider:
                provider_config = {"id": "bad-provider"}

                def __init__(self):
                    self.calls = 0

                async def text_chat(self, **kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        return LLMResponse(
                            role="assistant",
                            completion_text="",
                            tools_call_name=[
                                "other_tool" if kind == "other" else FETCH_TOOL_NAME
                            ],
                            tools_call_args=[{
                                "url": (
                                    "https://attacker.example"
                                    if kind == "wrong-url"
                                    else handle.url
                                )
                            }],
                            tools_call_ids=["bad-1"],
                        )
                    return LLMResponse(
                        role="assistant",
                        completion_text="",
                        tools_call_name=[FETCH_TOOL_NAME],
                        tools_call_args=[{"url": handle.url}],
                        tools_call_ids=["bad-2"],
                    )

            class Context:
                @staticmethod
                def get_config(umo=None):
                    return {"provider_settings": {"fallback_chat_models": []}}

                @staticmethod
                def get_provider_by_id(_provider_id):
                    return None

            service = TavernService(object(), Context(), {
                "url_request_enabled": True,
                "url_request_public_base_url": "https://public.example",
            })
            service.url_requests = broker
            try:
                with self.assertRaises(URLRequestProtocolError):
                    await service._url_chat_completion_with_fallback(
                        session_id="bad-session",
                        provider=Provider(),
                        handle=handle,
                    )
            finally:
                await broker.delete(handle)
                await broker.stop()

        run(scenario("other"))
        run(scenario("wrong-url"))
        run(scenario("repeated"))

    def test_url_request_provider_fallback_restarts_from_first_step(self):
        async def scenario():
            broker = URLRequestBroker(
                enabled=True,
                public_base_url="https://public.example",
                listen_host="127.0.0.1",
                listen_port=0,
                ttl_seconds=30,
            )
            await broker.start()
            handle = await broker.create([{"role": "user", "content": "fallback body"}])
            calls = []

            class Provider:
                def __init__(self, provider_id, fail=False):
                    self.provider_config = {"id": provider_id}
                    self.fail = fail

                async def text_chat(self, **kwargs):
                    calls.append((self.provider_config["id"], kwargs))
                    if self.fail:
                        raise RuntimeError("primary unavailable")
                    return LLMResponse(
                        role="assistant",
                        completion_text="fallback answer",
                    )

            primary = Provider("primary", fail=True)
            fallback = Provider("fallback")

            class Context:
                @staticmethod
                def get_config(umo=None):
                    return {
                        "provider_settings": {
                            "fallback_chat_models": ["fallback"],
                        }
                    }

                @staticmethod
                def get_provider_by_id(provider_id):
                    return fallback if provider_id == "fallback" else None

            service = TavernService(object(), Context(), {
                "url_request_enabled": True,
                "url_request_public_base_url": "https://public.example",
            })
            service.url_requests = broker
            try:
                response, provider_id = await service._url_chat_completion_with_fallback(
                    session_id="fallback-session",
                    provider=primary,
                    handle=handle,
                )
                self.assertEqual(response.completion_text, "fallback answer")
                self.assertEqual(provider_id, "fallback")
                self.assertEqual(
                    [provider_id for provider_id, _ in calls],
                    ["primary", "fallback"],
                )
                self.assertEqual(calls[0][1]["prompt"], calls[1][1]["prompt"])
                self.assertEqual(calls[1][1]["contexts"], [])
            finally:
                await broker.delete(handle)
                await broker.stop()

        run(scenario())

    def test_cipher_codecs_roundtrip_unicode_and_whitespace(self):
        text = "中文 English 😀\n  保留空白\t"
        for method in (
            "base64_utf8",
            "hex_utf8",
            "reverse_base64",
            "rot47",
            "unicode_shift_3",
            "xor_base64",
            "base91",
            "json_escape",
        ):
            with self.subTest(method=method):
                codec = CipherCodec(method)
                encoded = codec.encode_content(text)
                self.assertEqual(codec.decode_response(encoded).text, text)

    def test_unconventional_cipher_formats_and_protocols_are_explicit(self):
        text = "A中😀\n"
        rot47 = CipherCodec("rot47")
        self.assertNotIn("中", rot47.encode_payload(text))
        self.assertIn("JSON Unicode escape", rot47.protocol_prompt)
        self.assertIn("ROT47", rot47.protocol_prompt)

        shifted = CipherCodec("unicode_shift_3")
        shifted_payload = shifted.encode_payload(text)
        self.assertEqual(ord(shifted_payload[0]), ord("A") + 3)
        self.assertEqual(ord(shifted_payload[1]), ord("中") + 3)
        self.assertEqual(ord(shifted_payload[2]), ord("😀") + 3)
        self.assertEqual(shifted.encode_payload(chr(0xD7FF)), chr(0xE002))
        self.assertEqual(shifted.encode_payload(chr(0x10FFFF)), chr(2))
        self.assertEqual(
            shifted.decode_payload(shifted.encode_payload(chr(0x10FFFF))),
            chr(0x10FFFF),
        )
        self.assertIn("surrogate range U+D800 through U+DFFF", shifted.protocol_prompt)

        xor = CipherCodec("xor_base64")
        self.assertNotIn(text, xor.encode_payload(text))
        self.assertIn("'Komeiji'", xor.protocol_prompt)

        base91 = CipherCodec("base91")
        self.assertEqual(base91.encode_payload("test"), "fPNKd")
        self.assertIn("standard basE91", base91.protocol_prompt)

        escaped = CipherCodec("json_escape")
        escaped_payload = escaped.encode_payload(text)
        self.assertTrue(escaped_payload.startswith('"'))
        self.assertIn("\\u4e2d", escaped_payload)
        self.assertIn("\\ud83d\\ude00", escaped_payload)

    def test_unconventional_cipher_rejects_invalid_payloads(self):
        invalid_by_method = {
            "rot47": "plain text",
            "xor_base64": "not-valid!",
            "base91": "\\",
            "json_escape": '"unterminated',
        }
        for method, payload in invalid_by_method.items():
            with self.subTest(method=method):
                result = CipherCodec(method).decode_response(
                    f"<{CIPHER_TAG}>{payload}</{CIPHER_TAG}>"
                )
                self.assertFalse(result.ok)
                self.assertIn("载荷", result.error)

    def test_cipher_response_accepts_one_wrapped_payload_and_rejects_invalid_forms(self):
        codec = CipherCodec("base64_utf8")
        encoded = codec.encode_content("正常回复")
        fenced = f"模型说明\n```text\n{encoded}\n```"
        self.assertEqual(codec.decode_response(fenced).text, "正常回复")
        for invalid in (
            "plaintext",
            f"<{CIPHER_TAG}>not-valid!</{CIPHER_TAG}>",
            f"<{CIPHER_TAG}></{CIPHER_TAG}>",
            encoded + encoded,
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(codec.decode_response(invalid).ok)
        hex_reply = CipherCodec("hex_utf8").encode_content("算法不匹配")
        self.assertFalse(codec.decode_response(hex_reply).ok)

    def test_reverse_base64_recovers_small_utf8_damage(self):
        codec = CipherCodec("reverse_base64")
        reversed_bytes = bytearray("abc中文def"[::-1].encode("utf-8"))
        chinese_start = reversed_bytes.index("文".encode("utf-8"))
        reversed_bytes[chinese_start + 1] = 0xFF
        payload = base64.b64encode(reversed_bytes).decode("ascii")

        result = codec.decode_response(
            f"<{CIPHER_TAG}>{payload}</{CIPHER_TAG}>"
        )

        self.assertTrue(result.ok)
        self.assertTrue(result.degraded)
        self.assertIn("invalid UTF-8", result.error)
        self.assertTrue(result.text.startswith("abc"))
        self.assertTrue(result.text.endswith("def"))
        self.assertIn("\ufffd", result.text)

    def test_reverse_base64_rejects_heavily_corrupted_utf8(self):
        codec = CipherCodec("reverse_base64")
        payload = base64.b64encode(b"\xff" * 40).decode("ascii")

        result = codec.decode_response(
            f"<{CIPHER_TAG}>{payload}</{CIPHER_TAG}>"
        )

        self.assertFalse(result.ok)

    def test_cipher_provider_request_keeps_only_protocol_plaintext(self):
        service = TavernService(object(), object(), {
            "cipher_enabled": True,
            "cipher_method": "hex_utf8",
        })
        result = PromptBuilder().build(
            original_system="system secret",
            contexts=[
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            current_prompt="new question",
            preset={},
            character=None,
            persona="",
            lore=ScanResult(),
            values={},
        )
        system_prompt, contexts, prompt = service.provider_request_parts(result)
        self.assertIn("Encoding method: hex_utf8", system_prompt)
        self.assertNotIn("system secret", system_prompt)
        self.assertNotIn("old question", json.dumps(contexts, ensure_ascii=False))
        self.assertNotIn("old answer", json.dumps(contexts, ensure_ascii=False))
        self.assertNotIn("new question", prompt)
        self.assertTrue(prompt.startswith(f"<{CIPHER_TAG}>"))
        self.assertEqual([item["role"] for item in contexts], ["user", "assistant"])

    def test_cipher_disabled_preserves_provider_request_shape(self):
        service = TavernService(object(), object(), {"cipher_enabled": False})
        result = PromptBuilder().build(
            original_system="system",
            contexts=[{"role": "assistant", "content": "history"}],
            current_prompt="hello",
            preset={},
            character=None,
            persona="",
            lore=ScanResult(),
            values={},
        )
        self.assertEqual(
            service.provider_request_parts(result),
            ("system", [{"role": "assistant", "content": "history"}], "hello"),
        )

    def test_cipher_decode_failure_keeps_raw_model_output(self):
        service = TavernService(object(), object(), {
            "cipher_enabled": True,
            "cipher_method": "base64_utf8",
        })
        text, ok, error = service.decode_model_response("plain model reply")
        self.assertFalse(ok)
        self.assertIn("密文回复自动解码失败", text)
        self.assertIn("plain model reply", text)
        self.assertTrue(error)

    def test_cipher_mode_keeps_request_plaintext_and_skips_delivery_tool(self):
        logical = BuildResult(
            system_prompt="plain system",
            contexts=[{"role": "assistant", "content": "plain history"}],
            blocks=[],
            dropped=[],
            warnings=[],
            messages=[
                {"role": "system", "content": "plain system"},
                {"role": "assistant", "content": "plain history"},
                {"role": "user", "content": "plain prompt"},
            ],
            current_prompt="plain prompt",
        )

        class Service:
            cipher_enabled = True

            async def process(self, _event, _req):
                return logical

        class Tools:
            @staticmethod
            def get_func(_name):
                raise AssertionError("密文模式不应访问发送工具")

        class Event:
            unified_msg_origin = "s1"

            def __init__(self):
                self.extras = {}

            def set_extra(self, key, value):
                self.extras[key] = value

        req = _MockReq()
        req.func_tool = Tools()
        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.context = object()
        plugin.config = {"tool_delivery_enabled": True}
        plugin.service = Service()
        event = Event()
        run(plugin.on_llm_request(event, req))
        self.assertEqual(req.system_prompt, "plain system")
        self.assertEqual(req.contexts, [{"role": "assistant", "content": "plain history"}])
        self.assertEqual(req.prompt, "plain prompt")
        self.assertTrue(event.extras["_kt_cipher_active"])

    def test_cipher_agent_lifecycle_only_encodes_provider_messages(self):
        service = TavernService(object(), object(), {
            "cipher_enabled": True,
            "cipher_method": "reverse_base64",
        })
        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.config = {"status_bar_enabled": False}
        plugin.service = service

        class Event:
            def __init__(self):
                self.extras = {"_kt_cipher_active": True}

            def set_extra(self, key, value):
                self.extras[key] = value

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

        class RunContext:
            def __init__(self):
                self.messages = [
                    Message(role="system", content="plain system"),
                    Message(role="assistant", content="plain history"),
                    Message(
                        role="user",
                        content=[
                            TextPart(text="plain prompt"),
                            TextPart(text="\nplain reminder"),
                        ],
                    ),
                ]

        event = Event()
        run_context = RunContext()
        run(plugin.on_agent_begin(event, run_context))

        self.assertEqual(
            run_context.messages[0].content,
            service.cipher.protocol_prompt,
        )
        provider_dump = json.dumps(
            [message.model_dump() for message in run_context.messages],
            ensure_ascii=False,
        )
        self.assertNotIn("plain system", provider_dump)
        self.assertNotIn("plain history", provider_dump)
        self.assertNotIn("plain prompt", provider_dump)
        self.assertIn(CIPHER_TAG, provider_dump)

        raw_reply = service.cipher.encode_content("plain answer")
        run_context.messages.append(
            Message(role="assistant", content=raw_reply)
        )
        response = LLMResponse(role="assistant", completion_text=raw_reply)
        run(plugin.on_llm_response(event, response))
        self.assertEqual(response.completion_text, "plain answer")

        run(plugin.on_agent_done(event, run_context, response))

        persisted_dump = json.dumps(
            [message.model_dump() for message in run_context.messages],
            ensure_ascii=False,
        )
        self.assertNotIn(CIPHER_TAG, persisted_dump)
        self.assertNotIn(service.cipher.protocol_prompt, persisted_dump)
        self.assertIn("plain system", persisted_dump)
        self.assertIn("plain history", persisted_dump)
        self.assertIn("plain prompt", persisted_dump)
        self.assertIn("plain reminder", persisted_dump)
        self.assertIn("plain answer", persisted_dump)
        self.assertEqual(
            [message.role for message in run_context.messages],
            ["system", "assistant", "user", "assistant"],
        )

    def test_grouped_config_flattens_without_losing_values(self):
        grouped = {
            "cipher_config": {"cipher_enabled": True, "cipher_method": "hex_utf8"},
            "url_request_config": {
                "url_request_enabled": True,
                "url_request_backend": "hosted",
                "url_request_listen_port": 6190,
                "url_request_hosted_api_base_url": "https://relay.example",
                "url_request_hosted_timeout_seconds": 10,
                "url_request_hosted_proxy_url": "http://127.0.0.1:7897",
            },
            "context_config": {"history_max_messages": 12},
            "qq_forward_config": {"qq_forward_nodes_per_batch": 12},
            "summary_config": {"summary_enabled": True},
            "lifecycle_config": {"session_retention_days": 30},
        }
        flattened = _flatten_config(grouped)
        self.assertEqual(flattened["history_max_messages"], 12)
        self.assertTrue(flattened["cipher_enabled"])
        self.assertEqual(flattened["cipher_method"], "hex_utf8")
        self.assertTrue(flattened["url_request_enabled"])
        self.assertEqual(flattened["url_request_backend"], "hosted")
        self.assertEqual(flattened["url_request_listen_port"], 6190)
        self.assertEqual(
            flattened["url_request_hosted_api_base_url"],
            "https://relay.example",
        )
        self.assertEqual(flattened["url_request_hosted_timeout_seconds"], 10)
        self.assertEqual(
            flattened["url_request_hosted_proxy_url"],
            "http://127.0.0.1:7897",
        )
        self.assertEqual(flattened["qq_forward_nodes_per_batch"], 12)
        self.assertTrue(flattened["summary_enabled"])
        self.assertEqual(flattened["session_retention_days"], 30)

    def test_character_group_defaults_and_validation(self):
        normalized, errors, _ = validate_document("character_group", {})
        self.assertFalse(errors)
        self.assertEqual(normalized["members"], [])
        self.assertEqual(normalized["selection"], "round_robin")

        _, errors, _ = validate_document("character_group", {"members": {}, "selection": "bad"})
        self.assertTrue(any("members" in item for item in errors))
        self.assertTrue(any("selection" in item for item in errors))

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

    def test_remove_last_completed_turn_drops_user_assistant_and_tool_suffix(self):
        history = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": "kept reply"},
            {"role": "user", "content": "redo this"},
            {"role": "tool", "content": "tool result"},
            {"role": "assistant", "content": "bad reply"},
            {"role": "checkpoint", "content": "internal"},
        ]
        updated, removed = _remove_last_completed_turn(history)
        self.assertEqual(updated, history[:3])
        self.assertEqual(removed, 4)

    def test_latest_plain_turn_accepts_text_and_rejects_tools_or_media(self):
        history = [
            {"role": "user", "content": "keep"},
            {"role": "assistant", "content": "kept"},
            {"role": "user", "content": "redo"},
            {"role": "assistant", "content": "candidate one"},
        ]
        base, user, assistant = _latest_plain_turn(history)
        self.assertEqual(base, history[:2])
        self.assertEqual(user["content"], "redo")
        self.assertEqual(assistant["content"], "candidate one")
        self.assertIsNone(_latest_plain_turn([
            {"role": "user", "content": "redo"},
            {"role": "tool", "content": "result"},
            {"role": "assistant", "content": "reply"},
        ]))
        self.assertIsNone(_latest_plain_turn([
            {"role": "user", "content": [{"type": "image_url", "url": "x"}]},
            {"role": "assistant", "content": "reply"},
        ]))

    def test_tavern_undo_updates_astrbot_history_and_rolls_back_plugin_state(self):
        class Conversation:
            history = json.dumps([
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "first reply"},
                {"role": "user", "content": "bad direction"},
                {"role": "assistant", "content": "bad reply"},
            ])

        class Manager:
            def __init__(self):
                self.updated = None

            async def get_curr_conversation_id(self, _origin):
                return "cid-1"

            async def get_conversation(self, _origin, _cid):
                return Conversation()

            async def update_conversation(self, origin, cid, history):
                self.updated = (origin, cid, history)

        class Service:
            def __init__(self):
                self.rolled_back = []

            async def rollback_history_state(self, session_id):
                self.rolled_back.append(session_id)

        class Event:
            unified_msg_origin = "platform:private:user"

            @staticmethod
            def plain_result(text):
                return MessageEventResult().message(text)

        async def collect(generator):
            return [item async for item in generator]

        manager = Manager()
        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.context = type("Context", (), {"conversation_manager": manager})()
        plugin.service = Service()
        plugin._session_id = lambda _event: "test-session"

        results = run(collect(plugin.tavern(Event(), "undo", "")))
        self.assertEqual(manager.updated[2], [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "first reply"},
        ])
        self.assertEqual(plugin.service.rolled_back, ["test-session"])
        self.assertIn("已撤回上一轮对话", results[0].chain[0].text)

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

    def test_tv_alias_and_mentioned_tv_use_same_tavern_handler(self):
        class Event:
            unified_msg_origin = "platform:private:user"

            def __init__(self):
                self.target = "bot-self-id"

            @staticmethod
            def get_self_id():
                return "bot-self-id"

            def get_messages(self):
                return [At(qq=self.target), Plain("/tv status")]

            @staticmethod
            def get_message_str():
                return "/tv status"

            @staticmethod
            def plain_result(text):
                return MessageEventResult().message(text)

        class Storage:
            @staticmethod
            def get_session(_session_id):
                return {"turn": 2, "effects": {}}

        async def collect(generator):
            return [item async for item in generator]

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.storage = Storage()
        plugin._session_id = lambda _event: "test-session"

        direct = run(collect(plugin.tv(Event(), "status", "")))
        mentioned = run(collect(plugin.tavern_mentioned(Event())))
        self.assertEqual(direct[0].chain[0].text, mentioned[0].chain[0].text)
        self.assertIn("轮次：2", direct[0].chain[0].text)

    def test_tv_sw_alias_replays_original_prompt_not_command_text(self):
        class Conversation:
            cid = "cid-1"
            history = json.dumps([
                {"role": "user", "content": "open the door"},
                {"role": "assistant", "content": "first reply"},
            ])

        class Manager:
            async def get_curr_conversation_id(self, _origin):
                return "cid-1"

            async def get_conversation(self, _origin, _cid):
                return Conversation()

        class Service:
            async def candidate_group(self, _session_id):
                return None

            async def prepare_swipe(self, **kwargs):
                return {
                    "group_id": "g1",
                    "parent_node_id": "",
                    "candidate_index": 2,
                    "base_history": [],
                    "user_message": {"role": "user", "content": "open the door"},
                }

        class Event:
            unified_msg_origin = "platform:private:user"

            def __init__(self):
                self.extras = {}

            def set_extra(self, key, value):
                self.extras[key] = value

            @staticmethod
            def plain_result(text):
                return MessageEventResult().message(text)

            @staticmethod
            def request_llm(**kwargs):
                return kwargs

        async def collect(generator):
            return [item async for item in generator]

        plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
        plugin.context = type("Context", (), {"conversation_manager": Manager()})()
        plugin.service = Service()
        plugin._session_id = lambda _event: "session-1"
        event = Event()
        results = run(collect(plugin.tv(event, "sw", "")))
        self.assertEqual(results[0]["prompt"], "open the door")
        self.assertEqual(json.loads(results[0]["conversation"].history), [])
        self.assertEqual(
            event.extras["_kt_swipe_generation"]["candidate_index"], 2
        )

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

    def test_cipher_budget_accounts_for_encoded_expansion(self):
        plain = PromptBuilder(
            context_budget=2048,
            output_reserve=256,
            history_keep_recent_messages=0,
        )
        codec = CipherCodec("hex_utf8")
        ciphered = PromptBuilder(
            context_budget=2048,
            output_reserve=256,
            history_keep_recent_messages=0,
            content_token_estimator=lambda text: estimate_tokens(codec.encode_payload(text)),
            request_overhead_tokens=estimate_tokens(codec.protocol_prompt),
            message_overhead_tokens=estimate_tokens(codec.encode_content("")),
        )
        history = [
            {"role": "user", "content": ("中文和 English " * 80) + str(index)}
            for index in range(8)
        ]
        kwargs = dict(
            original_system="main",
            contexts=history,
            current_prompt="now",
            preset={},
            character=None,
            persona="",
            lore=ScanResult(),
            values={},
        )
        plain_result = plain.build(**kwargs)
        cipher_result = ciphered.build(**kwargs)
        self.assertLess(len(cipher_result.contexts), len(plain_result.contexts))

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

    def test_story_nodes_roundtrip_and_reset_preserves_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            storage.save_session("s1", {"turn": 3})
            node_id = storage.create_story_node({
                "session_id": "s1",
                "parent_id": "root",
                "branch_name": "if线",
                "title": "雨夜",
                "turn_index": 3,
                "request_messages": [{"role": "user", "content": "hello"}],
                "preview_payload": {"warnings": []},
                "assistant_text": "reply",
                "bindings_snapshot": {"single": {}},
                "retrieval_snapshot": {"matches": []},
                "memory_snapshot": {"matches": []},
                "state_snapshot": {"turn": 3},
            })
            node = storage.get_story_node(node_id)
            self.assertEqual(node["request_messages"][0]["content"], "hello")
            self.assertEqual(node["bindings_snapshot"], {"single": {}})
            listed = storage.list_story_nodes("s1")
            self.assertEqual(listed[0]["assistant_preview"], "reply")
            self.assertNotIn("request_messages", listed[0])
            self.assertTrue(storage.rename_story_node(node_id, title="新标题", branch_name="主线"))
            renamed = storage.get_story_node(node_id)
            self.assertEqual(renamed["title"], "新标题")
            self.assertEqual(renamed["branch_name"], "主线")
            storage.reset_session("s1")
            self.assertEqual(storage.get_session("s1")["turn"], 0)
            self.assertIsNotNone(storage.get_story_node(node_id))

    def test_candidate_group_storage_and_service_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            first_id = storage.create_story_node({
                "session_id": "s1",
                "title": "turn",
                "turn_index": 1,
                "request_messages": [{"role": "user", "content": "hello"}],
                "assistant_text": "first",
                "state_snapshot": {"turn": 1, "variables": {"choice": 1}},
                "base_state_snapshot": {"turn": 0, "variables": {}},
                "base_campaign_snapshot": {},
            })
            storage.save_session("s1", {
                "turn": 1,
                "current_story_node_id": first_id,
            })
            service = TavernService(storage, object(), {"swipe_candidate_limit": 5})
            prepared = run(service.prepare_swipe(
                session_id="s1",
                conversation_id="c1",
                base_history=[],
                user_message={"role": "user", "content": "hello"},
            ))
            self.assertEqual(prepared["candidate_index"], 2)
            self.assertEqual(storage.get_session("s1")["turn"], 0)
            second_id = storage.create_story_node({
                "session_id": "s1",
                "turn_index": 1,
                "request_messages": [{"role": "user", "content": "hello"}],
                "assistant_text": "second",
                "state_snapshot": {"turn": 1, "variables": {"choice": 2}},
                "candidate_group_id": prepared["group_id"],
                "candidate_index": 2,
                "candidate_selected": True,
            })
            storage.select_candidate_node(prepared["group_id"], second_id)
            selected = run(service.select_candidate("s1", 1))
            self.assertEqual(selected["assistant_text"], "first")
            self.assertEqual(
                storage.get_session("s1")["variables"]["choice"], 1
            )
            group = run(service.candidate_group("s1"))
            self.assertEqual(len(group["nodes"]), 2)
            self.assertTrue(group["nodes"][0]["candidate_selected"])

    def test_schema_v10_story_nodes_migrate_before_candidate_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            conn = sqlite3.connect(path)
            try:
                conn.executescript("""
                    CREATE TABLE story_nodes (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        parent_id TEXT NOT NULL DEFAULT '',
                        branch_name TEXT NOT NULL DEFAULT '',
                        title TEXT NOT NULL DEFAULT '',
                        turn_index INTEGER NOT NULL DEFAULT 0,
                        request_messages TEXT NOT NULL DEFAULT '[]',
                        preview_payload TEXT NOT NULL DEFAULT '{}',
                        assistant_text TEXT NOT NULL DEFAULT '',
                        assistant_payload TEXT NOT NULL DEFAULT '{}',
                        bindings_snapshot TEXT NOT NULL DEFAULT '{}',
                        retrieval_snapshot TEXT NOT NULL DEFAULT '{}',
                        memory_snapshot TEXT NOT NULL DEFAULT '{}',
                        state_snapshot TEXT NOT NULL DEFAULT '{}',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    INSERT INTO story_nodes(
                        id,session_id,created_at,updated_at
                    ) VALUES('legacy','s1',1,1);
                """)
                conn.commit()
            finally:
                conn.close()

            storage = TavernStorage(path)
            migrated = storage.get_story_node("legacy")
            self.assertEqual(migrated["candidate_group_id"], "")
            self.assertEqual(migrated["candidate_index"], 0)
            conn = sqlite3.connect(path)
            try:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(story_nodes)")
                }
                indexes = {
                    row[1] for row in conn.execute("PRAGMA index_list(story_nodes)")
                }
            finally:
                conn.close()
            self.assertIn("candidate_group_id", columns)
            self.assertIn("idx_story_nodes_candidate", indexes)

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

    def test_ensure_defaults_creates_and_binds_quick_replies(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            service = TavernService(storage, object(), {})
            service.ensure_defaults()

            documents = storage.list_documents("quick_reply")
            self.assertEqual(len(documents), 1)
            self.assertEqual(documents[0]["name"], "默认快捷回复")
            self.assertEqual(len(documents[0]["data"]["items"]), 6)
            self.assertEqual(documents[0]["data"]["items"][0]["label"], "继续剧情")
            bindings = storage.list_bindings(scope_type="global", scope_id="*", kind="quick_reply")
            self.assertEqual(bindings[0]["target_id"], documents[0]["id"])

            service.ensure_defaults()
            self.assertEqual(len(storage.list_documents("quick_reply")), 1)

    def test_finalize_story_snapshot_updates_current_node(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            storage.save_session("s1", {"turn": 2, "variables": {"mood": "calm"}})
            service = TavernService(storage, object(), {})
            node_id = run(service.finalize_story_snapshot({
                "session_id": "s1",
                "title": "节点",
                "turn_index": 1,
                "request_messages": [{"role": "user", "content": "hello"}],
                "state_snapshot": {"turn": 1},
            }, "reply", {"raw": True}))
            self.assertEqual(storage.get_session("s1")["current_story_node_id"], node_id)
            node = storage.get_story_node(node_id)
            self.assertEqual(node["assistant_text"], "reply")
            self.assertEqual(node["assistant_payload"], {"raw": True})
            self.assertEqual(node["state_snapshot"]["variables"], {"mood": "calm"})

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

    def test_message_text_strips_reasoning_parts_and_fields(self):
        message = {
            "role": "assistant",
            "content": {
                "type": "bot",
                "message": [
                    {"type": "think", "think": "internal chain"},
                    {"type": "reasoning", "text": "legacy chain"},
                    {"type": "plain", "text": "Visible answer."},
                ],
                "reasoning": "internal chainlegacy chain",
            },
        }
        self.assertEqual(TavernService._message_text(message), "Visible answer.")

    def test_process_strips_assistant_reasoning_from_real_request_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            storage.bind("global", "*", "preset", preset_id)
            service = TavernService(storage, object(), {})
            req = _MockReq()
            req.contexts = [
                {
                    "role": "assistant",
                    "content": {
                        "type": "bot",
                        "message": [
                            {"type": "think", "think": "secret reasoning"},
                            {"type": "plain", "text": "Visible answer."},
                        ],
                        "reasoning": "secret reasoning",
                        "reasoning_content": "secret reasoning",
                    },
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "plain", "text": "Follow-up question."},
                    ],
                },
            ]

            result = run(service.process(_MockEvent(), req))
            self.assertEqual(result.messages, [
                {"role": "system", "content": "base"},
                {"role": "assistant", "content": "Visible answer."},
                {"role": "user", "content": "Follow-up question."},
                {"role": "user", "content": "hello"},
            ])

            preview = storage.get_preview("s1")
            self.assertEqual(preview["messages"], result.messages)
            serialized = json.dumps(preview["messages"], ensure_ascii=False)
            self.assertNotIn("reasoning", serialized)
            self.assertNotIn("secret reasoning", serialized)

    def test_simulation_uses_character_group_selection_without_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            alice_id = storage.put_document("character", "Alice", {"data": {"name": "Alice", "description": "alice desc"}})
            bob_id = storage.put_document("character", "Bob", {"data": {"name": "Bob", "description": "bob desc"}})
            group_id = storage.put_document("character_group", "Group", {
                "members": [alice_id, bob_id], "selection": "round_robin",
            })
            storage.bind("global", "*", "preset", preset_id)
            storage.bind("session", "s1", "character_group", group_id)
            before = storage.get_session("s1")
            result = run(TavernService(storage, object(), {}).simulate({
                "session_id": "s1", "prompt": "hello", "system_prompt": "system"
            }))
            self.assertEqual(result["character_selection"]["character"]["card_name"], "Alice")
            self.assertEqual(result["character_selection"]["reason"], "round_robin")
            self.assertEqual(result["state_after"]["group_index"], 1)
            self.assertEqual(storage.get_session("s1"), before)

    def test_character_group_mentioned_and_manual_do_not_advance(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            preset_id = storage.put_document("preset", "Default", {"main_prompt": "base"})
            alice_id = storage.put_document("character", "Alice", {"data": {"name": "Alice"}})
            bob_id = storage.put_document("character", "Bob", {"data": {"name": "Bob"}})
            group_id = storage.put_document("character_group", "Group", {
                "members": [alice_id, bob_id], "selection": "manual",
            })
            storage.bind("global", "*", "preset", preset_id)
            storage.bind("session", "s1", "character_group", group_id)
            storage.save_session("s1", {"turn": 0, "effects": {}, "variables": {}, "group_index": 1})
            result = run(TavernService(storage, object(), {}).simulate({"session_id": "s1", "prompt": "Alice 请回答"}))
            self.assertEqual(result["character_selection"]["character"]["card_name"], "Alice")
            self.assertEqual(result["character_selection"]["reason"], "mentioned")
            self.assertEqual(result["state_after"]["group_index"], 1)

            result = run(TavernService(storage, object(), {}).simulate({"session_id": "s1", "prompt": "hello"}))
            self.assertEqual(result["character_selection"]["character"]["card_name"], "Bob")
            self.assertEqual(result["character_selection"]["reason"], "manual")
            self.assertEqual(result["state_after"]["group_index"], 1)

    def test_character_commands_lock_and_advance_group(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            alice_id = storage.put_document("character", "Alice", {"data": {"name": "Alice"}})
            bob_id = storage.put_document("character", "Bob", {"data": {"name": "Bob"}})
            group_id = storage.put_document("character_group", "Group", {
                "members": [alice_id, bob_id], "selection": "manual",
            })
            storage.bind("session", "s1", "character_group", group_id)
            service = TavernService(storage, object(), {})
            scopes = [("global", "*"), ("session", "s1")]

            self.assertIn("当前角色：Alice", service.character_status(scopes, "s1"))
            self.assertIn("已锁定角色：Bob", service.character_use(scopes, "s1", "Bob"))
            state = storage.get_session("s1")
            self.assertEqual(state["forced_character_id"], bob_id)
            self.assertEqual(state["group_index"], 1)

            self.assertIn("已切换到：Alice", service.character_next(scopes, "s1"))
            state = storage.get_session("s1")
            self.assertNotIn("forced_character_id", state)
            self.assertEqual(state["group_index"], 0)

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

    def test_cipher_reply_is_decoded_before_status_archive_and_illustration(self):
        class Illustration:
            async def maybe_illustrate_text(self, event, text):
                raise AssertionError("配图应在消息发送后调度")

        with tempfile.TemporaryDirectory() as directory:
            storage = TavernStorage(Path(directory) / "state.db")
            service = TavernService(storage, object(), {
                "cipher_enabled": True,
                "cipher_method": "base64_utf8",
            })
            plugin = KomeijiTavernPlugin.__new__(KomeijiTavernPlugin)
            plugin.config = {
                "cipher_enabled": True,
                "status_bar_enabled": True,
                "status_bar_template": "STATUS:{content}",
                "illustration_enabled": True,
            }
            plugin.storage = storage
            plugin.service = service
            plugin.illustration = Illustration()
            event = _FakeEvent()
            event.unified_msg_origin = "cipher-session"
            event.set_extra("_kt_cipher_active", True)
            event.set_extra("_kt_story_snapshot", {
                "session_id": "cipher-session",
                "request_messages": [{"role": "user", "content": "明文请求"}],
            })
            plaintext = '正文内容\n[TAVERN_STATE] {"mood":"calm"}'
            response = _FakeResponse(service.cipher.encode_content(plaintext))

            run(plugin.on_llm_response(event, response))

            self.assertIn("正文内容", response.completion_text)
            self.assertIn("STATUS:mood: calm", response.completion_text)
            self.assertNotIn(CIPHER_TAG, response.completion_text)
            self.assertEqual(storage.get_session("cipher-session")["variables"]["mood"], "calm")
            nodes = storage.list_story_nodes("cipher-session")
            self.assertEqual(nodes[0]["assistant_preview"], response.completion_text)
            self.assertEqual(event.get_extra("_kt_illustration_text"), response.completion_text)


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

    def test_stale_summary_is_discarded_after_astrbot_reset(self):
        provider = _FakeSummaryProvider()
        with tempfile.TemporaryDirectory() as directory:
            service = TavernService(TavernStorage(Path(directory) / "state.db"), _FakeSummaryContext(provider), {
                "summary_enabled": True, "summary_trigger_messages": 18, "history_max_messages": 12,
            })
            old_messages = self.messages(18)
            state = {
                "history_summary": {
                    "content": "summary from the previous conversation",
                    "covered_until": TavernService._message_fingerprint(old_messages[5]),
                    "covered_messages": 6,
                    "updated_at": 100.0,
                    "provider_id": "summary",
                },
            }

            history, meta, warnings, apply_limit = run(service._prepare_history(
                [{"role": "user", "content": "first message after reset"}],
                state,
                session_id="s1",
                generate=True,
            ))

            self.assertEqual(history, [{"role": "user", "content": "first message after reset"}])
            self.assertEqual(meta["source"], "none")
            self.assertEqual(meta["content"], "")
            self.assertEqual(meta["covered_messages"], 0)
            self.assertNotIn("history_summary", state)
            self.assertFalse(provider.calls)
            self.assertFalse(warnings)
            self.assertFalse(apply_limit)

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
    def test_delete_auto_memories_for_turn_preserves_manual_and_other_turns(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            for content, source_type, turn in (
                ("bad plot", "auto_extract", 3),
                ("older plot", "auto_extract", 2),
                ("manual note", "manual", 3),
            ):
                storage.put_memory(
                    scope_type="session", scope_id="s1", category="plot", content=content,
                    source_type=source_type, source_session_id="s1", source_turn=turn,
                )
            self.assertEqual(storage.delete_auto_memories_for_turn("s1", 3), 1)
            contents = {item["content"] for item in storage.list_memories(scope_type="session", scope_id="s1")}
            self.assertEqual(contents, {"older plot", "manual note"})

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
            self.assertEqual(items[0]["status"], "active")
            self.assertEqual(items[0]["importance"], 1.0)
            self.assertEqual(items[0]["source_type"], "manual")
            self.assertTrue(items[0]["content_hash"])
            self.assertTrue(storage.set_memory_enabled(memory_id, False))
            self.assertFalse(storage.list_memories(scope_type="session", scope_id="s1")[0]["enabled"])
            self.assertEqual(storage.list_memories(scope_type="session", scope_id="s1")[0]["status"], "archived")
            self.assertTrue(storage.delete_memory(memory_id))
            self.assertEqual(storage.list_memories(scope_type="session", scope_id="s1"), [])

    def test_put_memory_reuses_exact_duplicate_in_same_scope(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            first_id = storage.put_memory(
                scope_type="session", scope_id="s1", category="plot",
                content="Gate opened", embedding=[1.0, 0.0], importance=1.0,
            )
            second_id = storage.put_memory(
                scope_type="session", scope_id="s1", category="plot",
                content="Gate opened", embedding=[0.5, 0.5], importance=2.0,
            )
            self.assertEqual(second_id, first_id)
            items = storage.list_memories(scope_type="session", scope_id="s1")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["embedding"], [0.5, 0.5])
            self.assertEqual(items[0]["importance"], 2.0)

    def test_put_memory_keeps_exact_duplicate_in_different_scope(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            first_id = storage.put_memory(
                scope_type="session", scope_id="s1", category="plot",
                content="Gate opened", embedding=[1.0, 0.0],
            )
            second_id = storage.put_memory(
                scope_type="session", scope_id="s2", category="plot",
                content="Gate opened", embedding=[1.0, 0.0],
            )
            self.assertNotEqual(second_id, first_id)
            self.assertEqual(len(storage.list_memories()), 2)

    def test_set_memory_status_many_updates_enabled_flags(self):
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            first_id = storage.put_memory(
                scope_type="session", scope_id="s1", category="plot",
                content="first", embedding=[1.0, 0.0], status="pending",
            )
            second_id = storage.put_memory(
                scope_type="session", scope_id="s1", category="plot",
                content="second", embedding=[1.0, 0.0], status="pending",
            )
            self.assertEqual(storage.set_memory_status_many([first_id, second_id], "active"), 2)
            items = storage.list_memories(scope_type="session", scope_id="s1")
            self.assertTrue(all(item["status"] == "active" and item["enabled"] for item in items))
            self.assertEqual(storage.set_memory_status_many([first_id], "rejected"), 1)
            item = next(item for item in storage.list_memories(scope_type="session", scope_id="s1") if item["id"] == first_id)
            self.assertEqual(item["status"], "rejected")
            self.assertFalse(item["enabled"])

    def test_pending_and_expired_memories_are_not_retrieved(self):
        provider = _FakeEmbeddingProvider("emb", {"tea": [1.0, 0.0]})
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="active memory", embedding=[1.0, 0.0], status="active",
            )
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="pending memory", embedding=[1.0, 0.0], status="pending",
            )
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="expired memory", embedding=[1.0, 0.0], expires_at=1,
            )
            service = TavernService(storage, _FakeEmbeddingContext([provider]), {
                "memory_enabled": True,
                "embedding_provider_id": "emb",
                "memory_top_k": 5,
            })
            context, matches = run(service._retrieve_memories(scopes=[("session", "s1")], text="tea"))
            self.assertIn("active memory", context)
            self.assertNotIn("pending memory", context)
            self.assertNotIn("expired memory", context)
            self.assertEqual([item["content"] for item in matches], ["active memory"])

    def test_memory_importance_affects_retrieval_order(self):
        provider = _FakeEmbeddingProvider("emb", {"tea": [1.0, 0.0]})
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="low importance", embedding=[1.0, 0.0], importance=1.0,
            )
            storage.put_memory(
                scope_type="session", scope_id="s1", category="preference",
                content="high importance", embedding=[0.8, 0.0], importance=2.0,
            )
            service = TavernService(storage, _FakeEmbeddingContext([provider]), {
                "memory_enabled": True,
                "embedding_provider_id": "emb",
                "memory_top_k": 2,
            })
            _, matches = run(service._retrieve_memories(scopes=[("session", "s1")], text="tea"))
            self.assertEqual(matches[0]["content"], "high importance")

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
            self.assertEqual(result["memory"]["injected_count"], 1)
            self.assertEqual(storage.list_metrics(), [])

    def test_memory_extraction_can_write_pending(self):
        embedding = _FakeEmbeddingProvider("emb", {"User likes tea": [1.0, 0.0]})
        memory_provider = _FakeMemoryProvider()
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            service = TavernService(storage, _FakeMemoryContext(embedding, memory_provider), {
                "memory_enabled": True,
                "memory_extract_interval": 1,
                "memory_extract_mode": "pending",
                "memory_provider_id": "memory",
                "embedding_provider_id": "emb",
            })
            state = {"turn": 1}
            written = run(service._extract_memories(
                session_id="s1",
                state=state,
                messages=[{"role": "user", "content": "hello"}],
            ))
            self.assertEqual(len(written), 1)
            item = storage.list_memories()[0]
            self.assertEqual(item["status"], "pending")
            self.assertFalse(item["enabled"])
            self.assertEqual(item["source_type"], "auto_extract")

    def test_memory_extraction_reuses_exact_duplicate(self):
        embedding = _FakeEmbeddingProvider("emb", {"User likes tea": [1.0, 0.0]})
        memory_provider = _FakeMemoryProvider()
        with tempfile.TemporaryDirectory() as d:
            storage = TavernStorage(Path(d) / "state.db")
            service = TavernService(storage, _FakeMemoryContext(embedding, memory_provider), {
                "memory_enabled": True,
                "memory_extract_interval": 1,
                "memory_provider_id": "memory",
                "embedding_provider_id": "emb",
            })
            first_state = {"turn": 1}
            first_written = run(service._extract_memories(
                session_id="s1",
                state=first_state,
                messages=[{"role": "user", "content": "hello"}],
            ))
            second_state = {"turn": 2}
            second_written = run(service._extract_memories(
                session_id="s1",
                state=second_state,
                messages=[{"role": "user", "content": "hello again"}],
            ))
            self.assertEqual(second_written, first_written)
            self.assertEqual(len(storage.list_memories(scope_type="session", scope_id="s1")), 1)

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
