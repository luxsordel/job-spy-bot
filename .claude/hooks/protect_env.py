"""PreToolUse: не даёт Claude читать или править .env с секретами.

.env.example и прочие файлы не трогаем.
"""

from __future__ import annotations

import json
import re
import sys

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path") or ""
if re.search(r"(^|[\\/])\.env(\.local|\.prod|\.production)?$", path):
    sys.stderr.reconfigure(encoding="utf-8")
    print(
        f"Доступ к {path} запрещён хуком: там токены бота, ИИ и админки. "
        "Новые настройки добавляйте в Settings и .env.example; значения пользователь "
        "вписывает в .env сам. Токен админки VPS берите grep-ом по VPS_ADMIN_TOKEN.",
        file=sys.stderr,
    )
    sys.exit(2)
