from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Any


_MACRO = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class MacroResolver:
    def render(self, text: str, values: dict[str, Any] | None = None) -> str:
        values = {str(k).lower(): v for k, v in (values or {}).items()}

        def replace(match: re.Match[str]) -> str:
            expression = match.group(1).strip()
            key, _, argument = expression.partition("::")
            key = key.strip().lower()
            if key in values:
                return str(values[key])
            if key == "outlet":
                outlets = values.get("outlets", {})
                if isinstance(outlets, dict):
                    value = outlets.get(argument.strip(), [])
                    return "\n\n".join(str(item) for item in value) if isinstance(value, list) else str(value)
                return ""
            if key == "date":
                return datetime.now().strftime(argument or "%Y-%m-%d")
            if key == "time":
                return datetime.now().strftime(argument or "%H:%M:%S")
            if key == "random":
                choices = [part.strip() for part in argument.split(",") if part.strip()]
                return random.choice(choices) if choices else ""
            if key == "roll":
                try:
                    low, high = (int(x.strip()) for x in argument.split(",", 1))
                    return str(random.randint(low, high))
                except (TypeError, ValueError):
                    return match.group(0)
            return match.group(0)

        return _MACRO.sub(replace, text or "")
