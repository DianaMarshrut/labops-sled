"""Sled capsule helper: meeting protocol -> tasks JSON -> Bitrix24 tasks / .ics reminders.

Standard library only. Three commands:

  parse  protocol.md [--people people.json] [--tz +05:00] [--default-time 18:00]
         Reads the filled protocol template and prints tasks and reminders as JSON.
  ics    plan.json [-o reminders.ics] [--duration 30]
         Builds an RFC 5545 calendar file with VALARM reminders from the parsed plan.
  b24    plan.json [--confirm] [--only 1,3]
         Without --confirm: shows what would be created, sends nothing.
         Results are kept next to the plan (plan.sent.json): created tasks are never sent
         twice; a run interrupted mid-request stops until the user checks Bitrix24.
         With --confirm: creates the tasks in Bitrix24 via tasks.task.add -- the only
         Bitrix24 method this script can call. The webhook link is read from the
         SLED_B24_WEBHOOK environment variable or ~/.config/sled/b24_webhook and is
         never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

B24_METHOD = "tasks.task.add"  # the only Bitrix24 method in this capsule
WEBHOOK_ENV = "SLED_B24_WEBHOOK"
WEBHOOK_FILE = Path.home() / ".config" / "sled" / "b24_webhook"
WEBHOOK_RE = re.compile(r"^(https://[^/\s]+/rest/\d+/[^/\s]+/)")
NOT_NAMED = "не назван"
PRODID = "-//labopsai//sled//RU"


@dataclass
class Task:
    """One task row from the protocol."""

    n: int
    title: str
    owner: str
    deadline: str | None
    target: str
    responsible_id: int | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class Reminder:
    """One reminder row from the protocol."""

    n: int
    title: str
    start: str | None
    alarms_min: list[int]
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- parse


def parse_offset(text: str) -> timezone:
    """Parse '+05:00', '+5', 'UTC+5' into a fixed timezone."""
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", text.strip(), re.I)
    if not m:
        raise ValueError(f"Не понял часовой пояс: {text!r}. Пример: +05:00")
    sign = 1 if m.group(1) == "+" else -1
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return timezone(sign * delta)


def parse_when(text: str, tz: timezone, default_time: str) -> datetime | None:
    """Parse 'ДД.ММ.ГГГГ[ ЧЧ:ММ]' into an aware datetime; None for 'не назван'."""
    text = text.strip()
    if not text or text.lower().startswith(NOT_NAMED):
        return None
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ ,]+(\d{1,2}):(\d{2}))?", text)
    if not m:
        raise ValueError(f"Срок не в формате ДД.ММ.ГГГГ ЧЧ:ММ: {text!r}")
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4):
        hour, minute = int(m.group(4)), int(m.group(5))
    else:
        hour, minute = (int(x) for x in default_time.split(":"))
    return datetime(year, month, day, hour, minute, tzinfo=tz)


def parse_alarms(text: str) -> list[int]:
    """Parse 'за 1 день, за 30 минут, за 2 часа' into minutes before start."""
    units = {"мин": 1, "час": 60, "ден": 1440, "дн": 1440, "сут": 1440, "недел": 10080}
    out: list[int] = []
    for num, unit in re.findall(r"(\d+)\s*([а-яё]+)", text.lower()):
        for prefix, mult in units.items():
            if unit.startswith(prefix):
                out.append(int(num) * mult)
                break
    return sorted(set(out), reverse=True)


def _split_row(line: str) -> list[str]:
    cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
    return [c.strip().replace("\\|", "|") for c in cells]


def _section_rows(text: str, heading: str) -> list[list[str]]:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    rows = []
    for line in m.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = _split_row(line)
        if not cells or not cells[0].isdigit():
            continue  # header or separator
        rows.append(cells)
    return rows


def parse_protocol(text: str, people: dict[str, int], tz: timezone,
                   default_time: str = "18:00") -> dict:
    """Turn a filled protocol template into a plan with tasks and reminders."""
    title_m = re.search(r"^# Протокол встречи:\s*(.+)$", text, re.M)
    date_m = re.search(r"^Дата:\s*([\d.]+(?:\s+\d{1,2}:\d{2})?)", text, re.M)
    meeting = {"title": title_m.group(1).strip() if title_m else "",
               "date": date_m.group(1).strip() if date_m else ""}
    tasks: list[Task] = []
    for cells in _section_rows(text, "Задачи"):
        cells += [""] * (5 - len(cells))
        task = Task(int(cells[0]), cells[1], cells[2], None, cells[4])
        if not task.title:
            task.problems.append("нет текста задачи")
        if not task.owner or task.owner.lower().startswith(NOT_NAMED):
            task.problems.append("ответственный не назван")
        elif people:
            task.responsible_id = people.get(task.owner)
            if task.responsible_id is None:
                task.problems.append(f"нет номера в Битрикс24 для «{task.owner}»")
        try:
            when = parse_when(cells[3], tz, default_time)
            task.deadline = when.isoformat() if when else None
            if when is None:
                task.problems.append("срок не назван")
        except ValueError as exc:
            task.problems.append(str(exc))
        tasks.append(task)
    reminders: list[Reminder] = []
    for cells in _section_rows(text, "Напоминания"):
        cells += [""] * (4 - len(cells))
        rem = Reminder(int(cells[0]), cells[1], None, parse_alarms(cells[3]))
        try:
            when = parse_when(cells[2], tz, default_time)
            rem.start = when.isoformat() if when else None
            if when is None:
                rem.problems.append("время не названо")
        except ValueError as exc:
            rem.problems.append(str(exc))
        if not rem.title:
            rem.problems.append("нет названия")
        reminders.append(rem)
    return {"meeting": meeting,
            "tasks": [asdict(t) for t in tasks],
            "reminders": [asdict(r) for r in reminders]}


# ---------------------------------------------------------------- ics


def ics_escape(text: str) -> str:
    """Escape a TEXT value per RFC 5545 section 3.3.11."""
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def ics_fold(line: str, limit: int = 75) -> str:
    """Fold a content line to at most `limit` octets without splitting UTF-8 characters."""
    out: list[str] = []
    current, size = "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        cap = limit if not out else limit - 1  # continuation lines start with a space
        if size + n > cap:
            out.append(current)
            current, size = "", 0
        current += ch
        size += n
    out.append(current)
    return "\r\n ".join(out)


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _trigger(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"-P{minutes // 1440}D"
    if minutes % 60 == 0:
        return f"-PT{minutes // 60}H"
    return f"-PT{minutes}M"


def build_ics(plan: dict, duration_min: int = 30, now: datetime | None = None,
              secret: str | None = None) -> str:
    """Build a calendar file: one VEVENT per reminder with a VALARM per offset.

    Webhook links (and the key of `secret`) are removed from every text value.
    """
    now = now or datetime.now(timezone.utc)
    meeting = plan.get("meeting", {})
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    count = 0
    for rem in plan.get("reminders", []):
        if not rem.get("start") or rem.get("problems"):
            continue
        start = datetime.fromisoformat(rem["start"])
        end = start + timedelta(minutes=duration_min)
        title = redact(rem["title"], secret)
        uid_src = f"{title}|{rem['start']}|{rem['n']}".encode("utf-8")
        desc = redact(rem.get("description") or (
            f"Из встречи «{meeting.get('title', '')}» от {meeting.get('date', '')}".strip()),
            secret)
        lines += ["BEGIN:VEVENT",
                  f"UID:{hashlib.sha256(uid_src).hexdigest()[:24]}@sled.labopsai",
                  f"DTSTAMP:{_utc(now)}",
                  f"DTSTART:{_utc(start)}",
                  f"DTEND:{_utc(end)}",
                  f"SUMMARY:{ics_escape(title)}",
                  f"DESCRIPTION:{ics_escape(desc)}",
                  "STATUS:CONFIRMED", "SEQUENCE:0", "TRANSP:OPAQUE"]
        for minutes in rem.get("alarms_min") or []:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                      f"DESCRIPTION:{ics_escape('Напоминание: ' + title)}",
                      f"TRIGGER:{_trigger(int(minutes))}", "END:VALARM"]
        lines.append("END:VEVENT")
        count += 1
    lines.append("END:VCALENDAR")
    if not count:
        raise ValueError("Нет напоминаний с датой и временем — файл не собран")
    return "".join(ics_fold(line) + "\r\n" for line in lines)


# ---------------------------------------------------------------- b24


def load_webhook() -> str:
    """Read the tasks webhook base URL from the env var or the private file."""
    raw = os.environ.get(WEBHOOK_ENV, "").strip()
    if not raw and WEBHOOK_FILE.is_file():
        raw = WEBHOOK_FILE.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(f"Не нашёл ссылку вебхука: ни переменной {WEBHOOK_ENV}, "
                           f"ни файла ~/.config/sled/b24_webhook")
    m = WEBHOOK_RE.match(raw)
    if not m:
        raise RuntimeError("Ссылка вебхука не похожа на https://<портал>/rest/<номер>/<ключ>/")
    return m.group(1)


ANY_WEBHOOK_RE = re.compile(
    r"https?://[^\s/\"'<>]+/rest/\d+/[^\s/\"'<>]+/?[^\s\"'<>]*")


def redact(text: str, secret: str | None) -> str:
    """Remove webhook links (ours or any other) and our key from text before printing."""
    text = ANY_WEBHOOK_RE.sub("<вебхук>", str(text))
    if secret:
        key = secret.rstrip("/").rsplit("/", 1)[-1]
        if key:
            text = text.replace(key, "<ключ>")
    return text


def redact_obj(obj: object, secret: str | None) -> object:
    """Apply `redact` to every string inside nested lists and dicts."""
    if isinstance(obj, str):
        return redact(obj, secret)
    if isinstance(obj, list):
        return [redact_obj(x, secret) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v, secret) for k, v in obj.items()}
    return obj


def parse_only(raw: str | None, plan: dict) -> set[int] | None:
    """Strictly parse --only '1,3': existing, unique task numbers; ValueError otherwise."""
    if raw is None:
        return None
    raw = raw.replace(" ", "")
    if not re.fullmatch(r"\d+(?:,\d+)*", raw):
        raise ValueError("--only: нужны номера задач через запятую, например --only 1,3")
    nums = [int(x) for x in raw.split(",")]
    if len(nums) != len(set(nums)):
        raise ValueError("--only: номера повторяются")
    known = {t["n"] for t in plan.get("tasks", [])}
    missing = sorted(set(nums) - known)
    if missing:
        raise ValueError(f"--only: нет задач с номерами {missing}")
    return set(nums)


def select_tasks(plan: dict, only: set[int] | None) -> list[dict]:
    """Tasks meant for Bitrix24 (or exactly the numbers in `only`)."""
    tasks = plan.get("tasks", [])
    if only is not None:
        return [t for t in tasks if t["n"] in only]
    return [t for t in tasks if "битрикс" in (t.get("target") or "").lower()]


def check_task(task: dict) -> list[str]:
    """Independent check of the fields required right before sending."""
    problems = list(task.get("problems") or [])
    if not str(task.get("title") or "").strip():
        problems.append("нет текста задачи")
    rid = task.get("responsible_id")
    if not isinstance(rid, int) or isinstance(rid, bool) or rid <= 0:
        problems.append("нет номера ответственного в Битрикс24")
    deadline = task.get("deadline")
    if not deadline:
        problems.append("срок не назван")
    else:
        try:
            if datetime.fromisoformat(str(deadline)).tzinfo is None:
                problems.append("у срока нет часового пояса")
        except ValueError:
            problems.append("срок в неверном формате")
    return sorted(set(problems), key=problems.index)


def task_payload(task: dict, meeting: dict) -> dict:
    """Request body for tasks.task.add."""
    return {"fields": {
        "TITLE": task["title"],
        "RESPONSIBLE_ID": int(task["responsible_id"]),
        "DEADLINE": task["deadline"],
        "DESCRIPTION": f"Из встречи «{meeting.get('title', '')}» от {meeting.get('date', '')}",
    }}


def post_task(base: str, payload: dict, timeout: float = 30.0) -> dict:
    """POST one task to Bitrix24. The URL is built only from base + B24_METHOD."""
    req = urllib.request.Request(
        base + B24_METHOD, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
        return json.loads(resp.read().decode("utf-8"))


def _load_state(path: Path | None) -> dict[str, dict]:
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(path: Path | None, state: dict[str, dict]) -> None:
    """Write the state atomically: temp file, then rename over the old one."""
    if path is None:
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class StateLock:
    """Exclusive lock file next to the state, so two runs never send the same plan at once."""

    def __init__(self, state_path: Path | None) -> None:
        self.path = state_path.with_name(state_path.name + ".lock") if state_path else None
        self.fd: int | None = None

    def __enter__(self) -> "StateLock":
        if self.path is not None:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)  # type: ignore[union-attr]


def run_b24(plan: dict, confirm: bool, only: set[int] | None, out=sys.stdout,
            state_path: Path | None = None) -> int:
    """Show (dry run) or create the selected tasks. Returns a process exit code.

    `state_path` keeps the result per task number ("created" with id, "failed" -- Bitrix24
    clearly refused, "unknown" -- the task may exist), so a repeated run never re-sends
    a created task and stops on an unknown result. A lock file blocks parallel runs.
    """
    try:
        with StateLock(state_path):
            return _run_b24(plan, confirm, only, out, state_path)
    except FileExistsError:
        print(redact("Уже идёт другая отправка этого списка. Если другого запуска нет — удали "
                     f"файл {state_path.name}.lock рядом со списком и повтори.",  # type: ignore
                     _secret_or_none()), file=out)
        return 7


def _run_b24(plan: dict, confirm: bool, only: set[int] | None, out,
             state_path: Path | None) -> int:
    try:
        secret: str | None = load_webhook()
    except RuntimeError as exc:
        secret, webhook_error = None, str(exc)
    else:
        webhook_error = ""

    def say(text: str) -> None:
        print(redact(text, secret), file=out)

    tasks = select_tasks(plan, only)
    meeting = plan.get("meeting", {})
    if not tasks:
        say("Нет задач для Битрикс24 (в колонке «Где поставить» нет «Битрикс24»).")
        return 1
    state = _load_state(state_path)
    bad = False
    for t in tasks:
        problems = check_task(t)
        done = state.get(str(t["n"]), {})
        mark = "  !! " + "; ".join(problems) if problems else ""
        if done.get("status") == "created":
            mark += f"  (уже создана, №{done.get('id')})"
        elif done.get("status") == "unknown":
            mark += "  (?? результат прошлой отправки неизвестен)"
        say(f"{t['n']}. {t.get('title')} → {t.get('owner')}, срок "
            f"{t.get('deadline') or 'не назван'}{mark}")
        bad = bad or bool(problems)
    if bad:
        say("Есть задачи без ответственного, номера или срока — уточни и пересобери список. "
            "Ничего не отправлено.")
        return 2
    unknown = [t["n"] for t in tasks if state.get(str(t["n"]), {}).get("status") == "unknown"]
    if unknown:
        say(f"Задачи {unknown}: прошлая отправка оборвалась, результат неизвестен. Проверь "
            "в Битрикс24, создались ли они, и убери их из запуска (--only) или из файла "
            "состояния. Ничего не отправлено.")
        return 6
    if not confirm:
        say("Это просмотр. Ничего не отправлено. Для отправки — тот же запуск с --confirm "
            "после «да» пользователя.")
        return 0
    if secret is None:
        say(webhook_error)
        return 3
    code = 0
    for t in tasks:
        key = str(t["n"])
        if state.get(key, {}).get("status") == "created":
            say(f"{t['n']}. уже создана раньше (№{state[key].get('id')}), пропускаю")
            continue
        state[key] = {"status": "unknown"}
        _save_state(state_path, state)
        try:
            data = post_task(secret, task_payload(t, meeting))
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:  # Bitrix24 rejected the request: nothing was created
                state[key] = {"status": "failed"}
                say(f"{t['n']}. не создана: Битрикс24 отклонил запрос ({exc.code})")
            else:
                say(f"{t['n']}. результат неизвестен: ошибка сервера Битрикс24 ({exc.code}). "
                    "Проверь задачу в Битрикс24 перед повтором.")
            code = 4
        except (urllib.error.URLError, OSError, ValueError) as exc:
            say(f"{t['n']}. результат неизвестен: {exc}. Проверь задачу в Битрикс24 "
                "перед повтором.")
            code = 4
        else:
            task_id = (((data.get("result") or {}).get("task") or {}).get("id")
                       if isinstance(data, dict) else None)
            if task_id is not None and re.fullmatch(r"[1-9]\d{0,18}", str(task_id)):
                state[key] = {"status": "created", "id": str(task_id)}
                say(f"{t['n']}. создана задача №{task_id}: {t['title']}")
            elif isinstance(data, dict) and data.get("error"):
                err = data.get("error_description") or data.get("error")
                state[key] = {"status": "failed"}
                say(f"{t['n']}. не создана: {err}")
                code = 4
            else:
                say(f"{t['n']}. результат неизвестен: Битрикс24 не вернул номер задачи. "
                    "Проверь задачу в Битрикс24 перед повтором.")
                code = 4
        _save_state(state_path, state)
        if state[key]["status"] == "unknown":  # the task may exist: stop, send nothing more
            rest = [x["n"] for x in tasks[tasks.index(t) + 1:]
                    if state.get(str(x["n"]), {}).get("status") != "created"]
            if rest:
                say(f"Остановил отправку: задачи {rest} не отправлены. Сначала проверь в "
                    "Битрикс24 задачу с неизвестным результатом.")
            return 4
    return code


# ---------------------------------------------------------------- cli


def _secret_or_none() -> str | None:
    try:
        return load_webhook()
    except (RuntimeError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point. Nothing it prints or writes contains the webhook link."""
    secret = _secret_or_none()
    try:
        return _main(argv, secret)
    except Exception as exc:  # noqa: BLE001 -- last line of defence for the webhook
        print(redact(f"Ошибка: {exc}", secret), file=sys.stderr)
        return 1


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose help and error messages never show the webhook link."""

    secret: str | None = None

    def _print_message(self, message: str, file=None) -> None:  # noqa: ANN001
        if message:
            (file or sys.stderr).write(redact(message, _Parser.secret))


def _main(argv: list[str] | None, secret: str | None) -> int:
    _Parser.secret = secret
    ap = _Parser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse")
    p.add_argument("protocol", type=Path)
    p.add_argument("--people", type=Path)
    p.add_argument("--tz", default="+03:00")
    p.add_argument("--default-time", default="18:00")
    i = sub.add_parser("ics")
    i.add_argument("plan", type=Path)
    i.add_argument("-o", "--out", type=Path)
    i.add_argument("--duration", type=int, default=30)
    b = sub.add_parser("b24")
    b.add_argument("plan", type=Path)
    b.add_argument("--confirm", action="store_true")
    b.add_argument("--only", default=None)
    args = ap.parse_args(argv)

    if args.cmd == "parse":
        people = (json.loads(args.people.read_text(encoding="utf-8"))
                  if args.people else {})
        plan = parse_protocol(args.protocol.read_text(encoding="utf-8"), people,
                              parse_offset(args.tz), args.default_time)
        print(json.dumps(redact_obj(plan, secret), ensure_ascii=False, indent=2))
        return 0
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if args.cmd == "ics":
        text = build_ics(plan, args.duration, secret=secret)
        if args.out:
            args.out.write_bytes(text.encode("utf-8"))
            print(redact(f"Сохранил {args.out}", secret))
        else:
            sys.stdout.write(text)
        return 0
    try:
        only = parse_only(args.only, plan)
    except ValueError as exc:
        print(redact(str(exc), secret))
        return 5
    state = args.plan.with_name(args.plan.stem + ".sent.json")
    return run_b24(plan, args.confirm, only, state_path=state)


if __name__ == "__main__":
    sys.exit(main())
