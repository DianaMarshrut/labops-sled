"""Build distributables for the sled capsule.

Produces:
  dist/sled.zip           -- skill folder for upload to claude.ai.
  chat-instruction-sled.md -- merged instruction for ChatGPT / DeepSeek.

Usage: python3 scripts/build.py
"""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
SLED_DIR = REPO / "plugins/sled/skills/sled"
DIST = REPO / "dist"
ZIP_DATE = (2026, 1, 1, 0, 0, 0)

SLED_REFERENCES: list[tuple[str, str]] = [
    ("references/setup.md", "Приложение 1. Настройка один раз"),
    ("references/sources.md", "Приложение 2. Откуда берём встречу"),
    ("references/tasks.md", "Приложение 3. Задачи из протокола"),
    ("references/reminders.md", "Приложение 4. Напоминания"),
]
SLED_TEMPLATES: list[tuple[str, str, str]] = [
    ("templates/protocol.md", "Приложение 5. Шаблон протокола", "markdown"),
    ("templates/reminder.ics", "Приложение 6. Шаблон файла .ics", "text"),
]
SLED_SCRIPT_NOTE = "`sled.py` из плагина «След»"


def build_zip(skill_dir: Path, out: Path) -> Path:
    """Zip the skill folder so that the folder itself is the archive root."""
    out.parent.mkdir(parents=True, exist_ok=True)
    root = skill_dir.name
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(skill_dir.rglob("*")):
            if (path.is_file() and not path.name.startswith(".")
                    and "__pycache__" not in path.parts):
                info = zipfile.ZipInfo(f"{root}/{path.relative_to(skill_dir).as_posix()}",
                                       date_time=ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                info.create_system = 3
                zf.writestr(info, path.read_bytes())
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    listing = out.with_name(out.name + ".txt")
    listing.write_text(f"sha256 {digest}\n" + "\n".join(names) + "\n", encoding="utf-8")
    logger.info("Saved %s (%d files, sha256 %s)", out, len(names), digest[:12])
    return out


def strip_frontmatter(text: str) -> str:
    """Remove the YAML frontmatter block from a markdown file."""
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.S)


def _sled_links(text: str) -> str:
    """Point file references of the sled skill at appendices of the merged text."""
    for rel, title in SLED_REFERENCES + [(r, t) for r, t, _ in SLED_TEMPLATES]:
        text = text.replace(f"`{rel}`", f"«{title.split('. ', 1)[0]}» ниже")
    text = text.replace("`scripts/sled.py`", SLED_SCRIPT_NOTE)
    text = text.replace("scripts/sled.py", "sled.py")
    for old, new in (("шаблон «Приложение", "шаблон из «Приложения"),
                     ("по шаблону «Приложение", "по шаблону из «Приложения"),
                     ("Заполни «Приложение", "Заполни шаблон из «Приложения"),
                     ("по «Приложение ", "по «Приложению "),
                     ("из «Приложение ", "из «Приложения ")):
        text = text.replace(old, new)
    return text


def build_sled_chat_instruction(skill_dir: Path, out: Path) -> Path:
    """Merge the sled SKILL.md, references and templates into one chat instruction."""
    body = _sled_links(strip_frontmatter(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8")).strip())
    parts = [
        "# Инструкция для чата: капсула «След» от labopsai",
        "",
        "Вставь весь этот текст в инструкции проекта (Claude, ChatGPT) или первым сообщением "
        "в новый чат (DeepSeek и другие), либо приложи файлом и напиши: «Работай по этой "
        "инструкции». Затем — «разбери встречу» и текст встречи (или встреча из Granola, "
        "если она подключена).",
        "",
        "Ты работаешь в обычном чате. Встречу берёшь из встроенного коннектора (если он есть "
        "в этом чате) или из текста и файла, которые прислал пользователь. Других доступов "
        "к системам пользователя у тебя нет; ничего не записываешь без его «да». Скрипт "
        "`sled.py` есть только в плагине для Claude Code и в навыке Claude — в обычном чате "
        "делай то же по шаблонам и правилам из приложений.",
        "",
        "---",
        "",
        body,
    ]
    for rel, title in SLED_REFERENCES:
        ref = (skill_dir / rel).read_text(encoding="utf-8").strip()
        ref = _sled_links(re.sub(r"\A# .*\n", "", ref).strip())
        parts += ["", "---", "", f"# {title}", "", ref]
    for rel, title, lang in SLED_TEMPLATES:
        tpl = (skill_dir / rel).read_text(encoding="utf-8").replace("\r\n", "\n").strip()
        parts += ["", "---", "", f"# {title}", "", f"````{lang}", tpl, "````"]
    parts.append("")
    out.write_text("\n".join(parts), encoding="utf-8")
    logger.info("Saved %s (%d chars)", out, out.stat().st_size)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_zip(SLED_DIR, DIST / "sled.zip")
    build_sled_chat_instruction(SLED_DIR, REPO / "chat-instruction-sled.md")
