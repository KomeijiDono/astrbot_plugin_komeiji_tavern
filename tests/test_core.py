import asyncio
import json
import random
import tempfile
import unittest
import base64
import struct
import zlib
from pathlib import Path

from astrbot_plugin_komeiji_tavern.importers import detect_kind, export_document, parse_binary_payload, preview_import
from astrbot_plugin_komeiji_tavern.documents import validate_document
from astrbot_plugin_komeiji_tavern.lore import LoreScanner, normalize_entries
from astrbot_plugin_komeiji_tavern.models import Position, ScanResult
from astrbot_plugin_komeiji_tavern.prompt_builder import PromptBuilder
from astrbot_plugin_komeiji_tavern.qq_delivery import split_forward_text
from astrbot_plugin_komeiji_tavern.storage import TavernStorage
from astrbot_plugin_komeiji_tavern.service import TavernService
from astrbot_plugin_komeiji_tavern.web import TavernWebApi
from astrbot_plugin_komeiji_tavern.main import KomeijiTavernPlugin
from astrbot.api.message_components import Plain


def run(coro):
    return asyncio.run(coro)


def entry(uid, keys, content, **extra):
    return {"uid": uid, "key": keys, "content": content, **extra}


class CoreTests(unittest.TestCase):
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


 def test_import_round_trip(self):
    payload = {"entries": {"0": entry(0, ["key"], "content", extensions={"sticky": 2})}}
    self.assertEqual(detect_kind(payload), "lorebook")
    self.assertEqual(preview_import(payload)["count"], 1)
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

 def test_png_character_import(self):
    card = {"spec": "chara_card_v2", "data": {"name": "Alice", "first_mes": "Hello"}}
    text = b"chara\x00" + base64.b64encode(json.dumps(card).encode())
    chunk = struct.pack(">I", len(text)) + b"tEXt" + text + struct.pack(">I", zlib.crc32(b"tEXt" + text))
    end = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND"))
    png = b"\x89PNG\r\n\x1a\n" + chunk + end
    parsed = parse_binary_payload(base64.b64encode(png).decode(), "card.png")
    self.assertEqual(parsed["data"]["name"], "Alice")


if __name__ == "__main__":
    unittest.main()
