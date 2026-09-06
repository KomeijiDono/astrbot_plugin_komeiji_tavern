from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any


CIPHER_TAG = "KOMEIJI_CIPHER"
SUPPORTED_CIPHER_METHODS = {
    "base64_utf8",
    "hex_utf8",
    "reverse_base64",
    "rot47",
    "unicode_shift_3",
    "xor_base64",
    "base91",
    "json_escape",
}
DEFAULT_CIPHER_METHOD = "base64_utf8"
XOR_KEY = b"Komeiji"
UNICODE_SHIFT = 3
_UNICODE_SCALAR_COUNT = 0x110000 - 0x800
_BASE91_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    "!#$%&()*+,./:;<=>?@[]^_`{|}~\""
)
_BASE91_DECODE = {character: index for index, character in enumerate(_BASE91_ALPHABET)}
_CIPHER_PATTERN = re.compile(
    rf"<{CIPHER_TAG}>(.*?)</{CIPHER_TAG}>",
    re.DOTALL | re.IGNORECASE,
)


def normalize_cipher_method(method: str) -> str:
    normalized = str(method or DEFAULT_CIPHER_METHOD).strip().lower()
    return normalized if normalized in SUPPORTED_CIPHER_METHODS else DEFAULT_CIPHER_METHOD


def _rot47(text: str) -> str:
    return "".join(
        chr(33 + ((ord(character) - 33 + 47) % 94))
        if 33 <= ord(character) <= 126
        else character
        for character in text
    )


def _scalar_to_index(codepoint: int) -> int:
    if 0xD800 <= codepoint <= 0xDFFF:
        raise ValueError("Unicode surrogate is not a scalar value")
    return codepoint if codepoint < 0xD800 else codepoint - 0x800


def _index_to_scalar(index: int) -> int:
    return index if index < 0xD800 else index + 0x800


def _shift_unicode(text: str, amount: int) -> str:
    shifted: list[str] = []
    for character in text:
        index = _scalar_to_index(ord(character))
        shifted_index = (index + amount) % _UNICODE_SCALAR_COUNT
        shifted.append(chr(_index_to_scalar(shifted_index)))
    return "".join(shifted)


def _base91_encode(data: bytes) -> str:
    output: list[str] = []
    accumulator = 0
    bit_count = 0
    for byte in data:
        accumulator |= byte << bit_count
        bit_count += 8
        if bit_count <= 13:
            continue
        value = accumulator & 8191
        if value > 88:
            accumulator >>= 13
            bit_count -= 13
        else:
            value = accumulator & 16383
            accumulator >>= 14
            bit_count -= 14
        output.append(_BASE91_ALPHABET[value % 91])
        output.append(_BASE91_ALPHABET[value // 91])
    if bit_count:
        output.append(_BASE91_ALPHABET[accumulator % 91])
        if bit_count > 7 or accumulator > 90:
            output.append(_BASE91_ALPHABET[accumulator // 91])
    return "".join(output)


def _base91_decode(text: str) -> bytes:
    output = bytearray()
    accumulator = 0
    bit_count = 0
    pending = -1
    for character in text:
        try:
            value = _BASE91_DECODE[character]
        except KeyError as exc:
            raise ValueError("base91 载荷包含非法字符") from exc
        if pending < 0:
            pending = value
            continue
        pending += value * 91
        accumulator |= pending << bit_count
        bit_count += 13 if (pending & 8191) > 88 else 14
        while bit_count > 7:
            output.append(accumulator & 255)
            accumulator >>= 8
            bit_count -= 8
        pending = -1
    if pending >= 0:
        output.append((accumulator | pending << bit_count) & 255)
    return bytes(output)


@dataclass(frozen=True, slots=True)
class DecodeResult:
    text: str
    ok: bool
    error: str = ""
    degraded: bool = False


class CipherCodec:
    """Lightweight reversible encodings for model capability testing."""

    def __init__(self, method: str = DEFAULT_CIPHER_METHOD):
        self.method = normalize_cipher_method(method)

    @property
    def method_label(self) -> str:
        return {
            "base64_utf8": "UTF-8 Base64",
            "hex_utf8": "UTF-8 Hex",
            "reverse_base64": "Unicode reverse, then UTF-8 Base64",
            "rot47": "JSON Unicode escape, then ROT47",
            "unicode_shift_3": "Unicode scalar shift +3",
            "xor_base64": "UTF-8 XOR with fixed key, then Base64",
            "base91": "UTF-8 basE91",
            "json_escape": "JSON Unicode escape",
        }[self.method]

    @property
    def protocol_prompt(self) -> str:
        decode_steps = {
            "base64_utf8": (
                "Decode the Base64 payload to bytes, then decode those bytes as UTF-8."
            ),
            "hex_utf8": (
                "Decode the hexadecimal payload to bytes, then decode those bytes as UTF-8."
            ),
            "reverse_base64": (
                "Decode the Base64 payload to UTF-8 text, then reverse the Unicode characters."
            ),
            "rot47": (
                "Apply ROT47 to every printable ASCII character (code points 33 through 126), "
                "then parse the result as one JSON string. The JSON string uses ASCII Unicode "
                "escapes, including surrogate pairs for non-BMP characters."
            ),
            "unicode_shift_3": (
                "Subtract 3 from every Unicode scalar value with wraparound across all valid "
                "Unicode scalar values ordered as U+0000..U+D7FF then U+E000..U+10FFFF. "
                "The surrogate range U+D800 through U+DFFF is skipped. Treat the payload as "
                "literal shifted characters, not as JSON or backslash-u escape text."
            ),
            "xor_base64": (
                "Decode Base64 to bytes, XOR each byte with the repeating UTF-8/ASCII key "
                "'Komeiji' (hex 4b 6f 6d 65 69 6a 69), starting again at key byte 0 for each "
                "message, then decode the resulting bytes as UTF-8."
            ),
            "base91": (
                "Decode the payload with Joachim Henke's standard basE91, then decode the "
                f"resulting bytes as UTF-8. The exact alphabet in index order is "
                f"{_BASE91_ALPHABET!r}."
            ),
            "json_escape": (
                "Parse the complete payload as one JSON string. Interpret JSON escapes and UTF-16 "
                "surrogate pairs normally."
            ),
        }[self.method]
        encode_steps = {
            "base64_utf8": "UTF-8 encode your complete answer, then Base64 encode it.",
            "hex_utf8": "UTF-8 encode your complete answer, then hexadecimal encode it.",
            "reverse_base64": (
                "Reverse the Unicode characters of your complete answer, UTF-8 encode the result, "
                "then Base64 encode it."
            ),
            "rot47": (
                "Serialize your complete answer as one JSON string using ASCII-only Unicode escapes "
                "(keep the surrounding JSON quotes), then apply ROT47 to every printable ASCII "
                "character with code points 33 through 126."
            ),
            "unicode_shift_3": (
                "Add 3 to every Unicode scalar value with wraparound across all valid Unicode "
                "scalar values ordered as U+0000..U+D7FF then U+E000..U+10FFFF. Skip the "
                "surrogate range U+D800 through U+DFFF and output the shifted characters "
                "literally, not as JSON or backslash-u escapes."
            ),
            "xor_base64": (
                "UTF-8 encode your complete answer, XOR each byte with the repeating UTF-8/ASCII "
                "key 'Komeiji' (hex 4b 6f 6d 65 69 6a 69), starting at key byte 0, then Base64 "
                "encode the XOR result."
            ),
            "base91": (
                "UTF-8 encode your complete answer, then encode those bytes with Joachim Henke's "
                f"standard basE91 using this exact alphabet in index order: "
                f"{_BASE91_ALPHABET!r}."
            ),
            "json_escape": (
                "Serialize your complete answer as one JSON string using ASCII-only Unicode escapes "
                "and keep the surrounding JSON quotes."
            ),
        }[self.method]
        return (
            "This is a reversible-encoding capability test. It is not secure encryption.\n"
            f"Encoding method: {self.method} ({self.method_label}).\n"
            f"Every other message content is wrapped in <{CIPHER_TAG}>...</{CIPHER_TAG}>. "
            "Keep each message's original role and decode its wrapped payload before following it.\n"
            f"Decode procedure: {decode_steps}\n"
            "Respond to the decoded conversation normally, but do not output plaintext. "
            f"Encode only your complete final answer as follows: {encode_steps}\n"
            f"Return exactly one <{CIPHER_TAG}>encoded payload</{CIPHER_TAG}> block. "
            "A Markdown code fence around that block is allowed."
        )

    def encode_payload(self, text: str) -> str:
        value = str(text or "")
        if self.method == "hex_utf8":
            return value.encode("utf-8").hex()
        if self.method == "reverse_base64":
            value = value[::-1]
            return base64.b64encode(value.encode("utf-8")).decode("ascii")
        if self.method == "rot47":
            serialized = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
            return _rot47(serialized)
        if self.method == "unicode_shift_3":
            return _shift_unicode(value, UNICODE_SHIFT)
        if self.method == "xor_base64":
            data = value.encode("utf-8")
            encrypted = bytes(
                byte ^ XOR_KEY[index % len(XOR_KEY)]
                for index, byte in enumerate(data)
            )
            return base64.b64encode(encrypted).decode("ascii")
        if self.method == "base91":
            return _base91_encode(value.encode("utf-8"))
        if self.method == "json_escape":
            return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    def decode_payload(self, payload: str) -> str:
        raw = str(payload or "")
        if not raw:
            raise ValueError("密文载荷为空")
        compact = "".join(raw.split())
        try:
            if self.method == "hex_utf8":
                decoded = bytes.fromhex(compact).decode("utf-8")
            elif self.method == "reverse_base64":
                decoded = base64.b64decode(compact, validate=True).decode("utf-8")[::-1]
            elif self.method == "rot47":
                decoded = json.loads(_rot47(raw))
            elif self.method == "unicode_shift_3":
                decoded = _shift_unicode(raw, -UNICODE_SHIFT)
            elif self.method == "xor_base64":
                encrypted = base64.b64decode(compact, validate=True)
                data = bytes(
                    byte ^ XOR_KEY[index % len(XOR_KEY)]
                    for index, byte in enumerate(encrypted)
                )
                decoded = data.decode("utf-8")
            elif self.method == "base91":
                decoded = _base91_decode(compact).decode("utf-8")
            elif self.method == "json_escape":
                decoded = json.loads(raw)
            else:
                decoded = base64.b64decode(compact, validate=True).decode("utf-8")
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise ValueError(f"{self.method} 载荷无效") from exc
        if not isinstance(decoded, str):
            raise ValueError(f"{self.method} 载荷不是 JSON 字符串")
        return decoded

    def encode_content(self, text: str) -> str:
        return f"<{CIPHER_TAG}>{self.encode_payload(text)}</{CIPHER_TAG}>"

    def encode_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded = [{"role": "system", "content": self.protocol_prompt}]
        for message in messages:
            if not isinstance(message, dict):
                continue
            item = dict(message)
            item["role"] = str(message.get("role", "user") or "user")
            item["content"] = self.encode_content(str(message.get("content", "") or ""))
            encoded.append(item)
        return encoded

    def decode_response(self, text: str) -> DecodeResult:
        raw = str(text or "")
        matches = _CIPHER_PATTERN.findall(raw)
        if len(matches) != 1:
            reason = "未找到密文标签" if not matches else "检测到多个密文标签"
            return DecodeResult(raw, False, reason)
        payload = str(matches[0] or "")
        if self.method == "reverse_base64" and payload:
            try:
                decoded_bytes = base64.b64decode(
                    "".join(payload.split()),
                    validate=True,
                )
            except binascii.Error:
                pass
            else:
                try:
                    decoded_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    tolerant = decoded_bytes.decode("utf-8", errors="replace")
                    replacement_count = tolerant.count("\ufffd")
                    replacement_limit = max(8, len(tolerant) // 500)
                    if 0 < replacement_count <= replacement_limit:
                        warning = (
                            "reverse_base64 payload contained "
                            f"{replacement_count} invalid UTF-8 segment(s); "
                            "recovered with replacement characters"
                        )
                        return DecodeResult(
                            tolerant[::-1],
                            True,
                            warning,
                            degraded=True,
                        )
        try:
            decoded = self.decode_payload(payload)
        except ValueError as exc:
            return DecodeResult(raw, False, str(exc))
        return DecodeResult(decoded, True)

    def metadata(self, *, provider_encoded: bool) -> dict[str, Any]:
        return {
            "enabled": True,
            "method": self.method,
            "method_label": self.method_label,
            "scope": "all_message_content",
            "response_mode": "plugin_auto_decrypt",
            "provider_request_encoded": bool(provider_encoded),
            "stored_messages": "plaintext",
            "security_notice": "仅用于可逆编码能力测试，不提供安全加密。",
        }
