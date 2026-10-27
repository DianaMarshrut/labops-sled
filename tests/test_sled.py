"""Checks for the sled capsule: manifests, zip, chat text, protocol parsing, .ics, Bitrix24."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SKILL = REPO / "plugins/sled/skills/sled"
FIX = REPO / "tests/fixtures/sled"
VERSION = "0.1.0"
FAKE_KEY = "zz9secretkey77"
FAKE_WEBHOOK = f"https://example.bitrix24.ru/rest/42/{FAKE_KEY}/"

sys.dont_write_bytecode = True  # keep __pycache__ out of the skill folder
_spec = importlib.util.spec_from_file_location("sled_tool", SKILL / "scripts/sled.py")
sled = importlib.util.module_from_spec(_spec)
sys.modules["sled_tool"] = sled
_spec.loader.exec_module(sled)  # type: ignore[union-attr]
sys.path.insert(0, str(REPO / "scripts"))

icalendar = pytest.importorskip("icalendar")


def _skill_files() -> list[Path]:
    return sorted(f for f in SKILL.rglob("*")
                  if f.is_file() and "__pycache__" not in f.parts)


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md has no frontmatter"
    return dict(line.split(": ", 1) for line in m.group(1).splitlines())


def _plan() -> dict:
    people = json.loads((FIX / "people.json").read_text(encoding="utf-8"))
    return sled.parse_protocol((FIX / "protocol.md").read_text(encoding="utf-8"), people,
                               sled.parse_offset("+05:00"), "18:00")


# ---------------------------------------------------------------- packaging


def test_manifests_and_versions() -> None:
    mp = json.loads((REPO / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    pl = json.loads((REPO / "plugins/sled/.claude-plugin/plugin.json").read_text(encoding="utf-8"))
    entry = next(p for p in mp["plugins"] if p["name"] == "sled")
    assert entry["source"] == "./plugins/sled"
    assert pl["name"] == "sled"
    assert pl["version"] == entry["version"] == VERSION
    assert not {"hooks", "mcpServers", "commands", "agents"} & pl.keys()
    assert not (REPO / "plugins/sled/hooks").exists()
    assert len({p["name"] for p in mp["plugins"]}) == len(mp["plugins"])


def test_skill_frontmatter_limits() -> None:
    fm = _frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))
    assert fm["name"] == "sled"
    assert 0 < len(fm["description"]) <= 200  # claude.ai upload limit


def test_shipped_zip_matches_skill_and_checksum() -> None:
    zpath = REPO / "dist/sled.zip"
    listing = (REPO / "dist/sled.zip.txt").read_text(encoding="utf-8").split()
    assert listing[0] == "sha256"
    assert hashlib.sha256(zpath.read_bytes()).hexdigest() == listing[1]
    expected = sorted(f"sled/{f.relative_to(SKILL).as_posix()}" for f in _skill_files())
    with zipfile.ZipFile(zpath) as zf:
        assert sorted(zf.namelist()) == expected == sorted(listing[2:])
        for f in _skill_files():
            assert zf.read(f"sled/{f.relative_to(SKILL).as_posix()}") == f.read_bytes()


def test_chat_instruction_is_current(tmp_path: Path) -> None:
    import build

    fresh = build.build_sled_chat_instruction(SKILL, tmp_path / "c.md").read_text("utf-8")
    assert fresh == (REPO / "chat-instruction-sled.md").read_text(encoding="utf-8")


def test_chat_instruction_is_self_contained() -> None:
    text = (REPO / "chat-instruction-sled.md").read_text(encoding="utf-8")
    for bad in ("references/", "templates/", "scripts/", "ПРОВЕРИТЬ"):
        assert bad not in text
    assert "BEGIN:VCALENDAR" in text and "BEGIN:VALARM" in text
    assert "## Задачи" in text and "Настройки следа" in text


def test_one_time_vs_every_time_table() -> None:
    setup = (SKILL / "references/setup.md").read_text(encoding="utf-8")
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    for text in (setup, readme):
        assert re.search(r"\| *ИИ *\| *Один раз *\| *Каждый раз *\|", text)
        for ai in ("Claude", "ChatGPT", "DeepSeek"):
            assert re.search(rf"^\| {ai}", text, re.M), ai
        assert "разбери встречу" in text


# ---------------------------------------------------------------- protocol


def test_reference_protocol_matches_transcript() -> None:
    """The reference protocol uses only facts from the sample transcript."""
    transcript = (FIX / "transcript.txt").read_text(encoding="utf-8")
    plan = _plan()
    assert plan["meeting"] == {"title": "Планёрка отдела продаж", "date": "27.10.2026 10:00"}
    tasks = {t["n"]: t for t in plan["tasks"]}
    assert len(tasks) == 4
    for t in tasks.values():
        if t["owner"] != "не назван":
            assert t["owner"] in transcript
    # "до четверга" from Tue 27.10 -> 29.10, default time; "до пятницы, к 12:00" -> 30.10 12:00
    assert tasks[1]["deadline"] == "2026-10-29T18:00:00+05:00"
    assert tasks[2]["deadline"] == "2026-10-30T12:00:00+05:00"
    assert tasks[1]["responsible_id"] == 15 and tasks[2]["responsible_id"] == 27
    assert tasks[3]["problems"] == ["срок не назван"]
    assert "ответственный не назван" in tasks[4]["problems"]
    # The instruction spoken inside the meeting is data, not a task.
    joined = " ".join(t["title"].lower() for t in tasks.values())
    assert "удали" not in joined and "всем клиентам" not in joined
    assert "удали все задачи" in transcript
    rem = plan["reminders"][0]
    assert rem["start"] == "2026-10-30T11:00:00+05:00" and rem["alarms_min"] == [1440, 30]


def test_parse_helpers() -> None:
    assert sled.parse_alarms("за 1 день, за 30 минут, за 2 часа") == [1440, 120, 30]
    assert sled.parse_offset("UTC+5").utcoffset(None) == timedelta(hours=5)
    assert sled.parse_offset("-03:30").utcoffset(None) == -timedelta(hours=3, minutes=30)
    tz = sled.parse_offset("+03:00")
    assert sled.parse_when("не назван", tz, "18:00") is None
    with pytest.raises(ValueError):
        sled.parse_when("в пятницу", tz, "18:00")


def test_unknown_person_is_flagged() -> None:
    text = (FIX / "protocol.md").read_text(encoding="utf-8")
    plan = sled.parse_protocol(text, {"Иван Петров": 15}, sled.parse_offset("+05:00"))
    t2 = next(t for t in plan["tasks"] if t["n"] == 2)
    assert t2["responsible_id"] is None
    assert any("нет номера" in p for p in t2["problems"])


# ---------------------------------------------------------------- ics


def _check_ics(raw: bytes) -> list:
    assert raw.endswith(b"\r\n")
    lines = raw.split(b"\r\n")[:-1]
    assert all(b"\n" not in line for line in lines)
    assert max(len(line) for line in lines) <= 75
    raw.decode("utf-8")
    cal = icalendar.Calendar.from_ical(raw)
    assert str(cal["VERSION"]) == "2.0" and cal.get("PRODID")
    events = list(cal.walk("VEVENT"))
    for ev in events:
        for key in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            assert ev.get(key) is not None, key
        for al in ev.walk("VALARM"):
            assert str(al["ACTION"]) == "DISPLAY" and al.get("DESCRIPTION") and al.get("TRIGGER")
    return events


def test_ics_from_script_is_valid_with_alarms() -> None:
    now = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    raw = sled.build_ics(_plan(), 30, now).encode("utf-8")
    events = _check_ics(raw)
    assert len(events) == 1
    ev = events[0]
    assert ev.decoded("DTSTART") == datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    assert ev.decoded("DTEND") - ev.decoded("DTSTART") == timedelta(minutes=30)
    triggers = sorted(a.decoded("TRIGGER") for a in ev.walk("VALARM"))
    assert triggers == [timedelta(days=-1), timedelta(minutes=-30)]
    # escaping round trip: comma, semicolon, backslash survive
    assert str(ev["SUMMARY"]) == "Сверка по КП «Ромашка», пилот; бюджет \\ расчёт"
    assert "Планёрка отдела продаж" in str(ev["DESCRIPTION"])
    assert b"\\, " in raw and b"\\;" in raw and b"\\\\" in raw


def test_ics_escape_and_fold() -> None:
    assert sled.ics_escape("a,b;c\\d\ne") == "a\\,b\\;c\\\\d\\ne"
    long = "SUMMARY:" + "Ж" * 100 + ", конец"
    folded = sled.ics_fold(long)
    parts = folded.split("\r\n")
    assert all(len(p.encode("utf-8")) <= 75 for p in parts)
    assert all(p.startswith(" ") for p in parts[1:])
    assert "".join(p[1:] if i else p for i, p in enumerate(parts)) == long


def test_ics_refuses_empty_plan() -> None:
    with pytest.raises(ValueError):
        sled.build_ics({"meeting": {}, "reminders": [{"n": 1, "title": "x", "start": None,
                                                      "alarms_min": [], "problems": ["x"]}]})


def test_filled_template_is_valid() -> None:
    """The text template for chats without code parses when filled by its own rules."""
    tpl = (SKILL / "templates/reminder.ics").read_bytes().decode("utf-8")
    assert "\r\n" in tpl and "\n" not in tpl.replace("\r\n", "")
    values = {
        "{Europe/Moscow}": "Asia/Yekaterinburg", "{+0300}": "+0500", "{MSK}": "+05",
        "{20261030-1100-1}": "20261030-1100-1", "{20261027T090000Z}": "20261027T090000Z",
        "{20261030T110000}": "20261030T110000", "{20261030T113000}": "20261030T113000",
        "{Название события}": sled.ics_escape("Сверка по КП, пилот"),
        "{Задача — ответственный — срок}": sled.ics_escape("КП — Иван; до 29.10"),
        "{Завтра: название события}": "Завтра: сверка",
        "{Через 30 минут: название события}": "Через 30 минут: сверка",
    }
    for k, v in values.items():
        assert k in tpl, k
        tpl = tpl.replace(k, v)
    assert "{" not in tpl
    ev = _check_ics(tpl.encode("utf-8"))[0]
    start = ev.decoded("DTSTART")
    assert start.utcoffset() == timedelta(hours=5) and start.hour == 11
    assert len(list(ev.walk("VALARM"))) == 2
    assert str(ev["SUMMARY"]) == "Сверка по КП, пилот"


# ---------------------------------------------------------------- Bitrix24


class _Resp(io.BytesIO):
    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *a):  # noqa: D105
        return False


def _no_network(*a, **k):
    raise AssertionError("network must not be touched")


def test_b24_dry_run_sends_nothing(monkeypatch) -> None:
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    out = io.StringIO()
    assert sled.run_b24(_plan(), confirm=False, only={1, 2}, out=out) == 0
    assert "Ничего не отправлено" in out.getvalue()
    # tasks with a missing deadline or owner block the whole run, even with --confirm
    out = io.StringIO()
    assert sled.run_b24(_plan(), confirm=True, only=None, out=out) == 2
    assert "срок не назван" in out.getvalue()


def test_b24_confirm_posts_only_task_add(monkeypatch) -> None:
    calls = []

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        return _Resp(json.dumps({"result": {"task": {"id": str(100 + len(calls))}}}).encode())

    monkeypatch.setattr(sled.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK + "crm.deal.list.json")
    out = io.StringIO()
    assert sled.run_b24(_plan(), confirm=True, only={1, 2}, out=out) == 0
    assert len(calls) == 2
    for req in calls:
        assert req.get_method() == "POST"
        assert req.full_url == FAKE_WEBHOOK + "tasks.task.add"
    body = json.loads(calls[0].data.decode("utf-8"))
    assert body == {"fields": {
        "TITLE": "Подготовить и отправить КП клиенту «Ромашка»", "RESPONSIBLE_ID": 15,
        "DESCRIPTION": "Из встречи «Планёрка отдела продаж» от 27.10.2026 10:00",
        "DEADLINE": "2026-10-29T18:00:00+05:00"}}
    text = out.getvalue()
    assert "создана задача №101" in text and "№102" in text
    assert FAKE_KEY not in text and "bitrix24.ru" not in text


def test_b24_errors_never_print_webhook(monkeypatch) -> None:
    def failing(req, timeout=0):
        raise sled.urllib.error.URLError(f"cannot reach {req.full_url}")

    monkeypatch.setattr(sled.urllib.request, "urlopen", failing)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    out = io.StringIO()
    assert sled.run_b24(_plan(), confirm=True, only={1}, out=out) == 4
    assert FAKE_KEY not in out.getvalue() and "результат неизвестен" in out.getvalue()

    def api_error(req, timeout=0):
        return _Resp(json.dumps({"error": "x", "error_description": f"bad {FAKE_KEY}"}).encode())

    monkeypatch.setattr(sled.urllib.request, "urlopen", api_error)
    out = io.StringIO()
    assert sled.run_b24(_plan(), confirm=True, only={1}, out=out) == 4
    assert FAKE_KEY not in out.getvalue()


def test_b24_rejects_bad_webhook(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    monkeypatch.setattr(sled, "WEBHOOK_FILE", tmp_path / "missing")
    for bad in ("", "http://example.bitrix24.ru/rest/1/abc/", "https://example.com/x"):
        monkeypatch.setenv(sled.WEBHOOK_ENV, bad)
        out = io.StringIO()
        assert sled.run_b24(_plan(), confirm=True, only={1}, out=out) == 3


# ---------------------------------------------------------------- security


B24_METHOD_RE = re.compile(
    r"\b(?:tasks?|crm|calendar|user|im|disk|lists|entity|sonet_group|department|batch|"
    r"profile|imbot|placement|event)\.[a-z_]+(?:\.[a-z_]+)*\b")


def test_only_task_add_method_anywhere() -> None:
    """No Bitrix24 write or read method except tasks.task.add in the sled capsule."""
    assert sled.B24_METHOD == "tasks.task.add"
    texts = [f.read_text(encoding="utf-8") for f in _skill_files() if f.suffix != ".py"]
    texts.append((REPO / "chat-instruction-sled.md").read_text(encoding="utf-8"))
    tree = ast.parse((SKILL / "scripts/sled.py").read_text(encoding="utf-8"))
    texts += [n.value for n in ast.walk(tree)  # string literals of the script
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    found = set()
    for text in texts:
        found |= {m for m in B24_METHOD_RE.findall(text)
                  if not m.endswith((".md", ".py", ".json", ".ics"))}
    assert found == {"tasks.task.add"}, found
    src = (SKILL / "scripts/sled.py").read_text(encoding="utf-8")
    assert src.count("urllib.request.Request(") == 1
    assert "base + B24_METHOD" in src


def test_no_secrets_or_local_paths() -> None:
    files = _skill_files() + [REPO / "chat-instruction-sled.md", FIX / "protocol.md",
                              FIX / "transcript.txt"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"/rest/\d+/[A-Za-z0-9]{6,}/", text), f
        assert "http://" not in text, f
        assert not re.search(r"(?:^|\s)/(?:home|Users)/\w+", text), f  # no local paths


# ---------------------------------------------------------------- Bitrix24 hardening


def _ok_urlopen(calls: list):
    def fake(req, timeout=0):
        calls.append(req)
        return _Resp(json.dumps({"result": {"task": {"id": str(200 + len(calls))}}}).encode())
    return fake


def test_only_is_strict(monkeypatch, tmp_path: Path) -> None:
    plan = _plan()
    for bad in ("", "abc", "1,1", "9", "1;2", "-1"):
        with pytest.raises(ValueError):
            sled.parse_only(bad, plan)
    assert sled.parse_only(None, plan) is None
    assert sled.parse_only(" 1, 2 ", plan) == {1, 2}
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    assert sled.main(["b24", str(pf), "--confirm", "--only", "abc"]) == 5
    assert sled.main(["b24", str(pf), "--confirm", "--only", ""]) == 5


def test_missing_fields_checked_before_send(monkeypatch) -> None:
    """A hand-edited plan without a deadline (and no problems listed) is still refused."""
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    for patch in ({"deadline": None}, {"deadline": "2026-10-29T18:00:00"},
                  {"deadline": "завтра"}, {"responsible_id": None}, {"responsible_id": "15"},
                  {"title": " "}):
        plan = _plan()
        plan["tasks"][0].update(patch)
        plan["tasks"][0]["problems"] = []
        out = io.StringIO()
        assert sled.run_b24(plan, confirm=True, only={1}, out=out) == 2, patch


def test_all_output_is_redacted(monkeypatch) -> None:
    calls: list = []

    def leaky(req, timeout=0):
        calls.append(req)
        return _Resp(json.dumps({"result": {"task": {"id": "7"}}}).encode())

    monkeypatch.setattr(sled.urllib.request, "urlopen", leaky)
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    plan = _plan()
    plan["tasks"][0]["title"] = f"Проверить {FAKE_WEBHOOK}tasks.task.add и ещё"
    plan["tasks"][1]["owner"] = "https://other.bitrix24.ru/rest/5/osk77/"
    for confirm in (False, True):
        out = io.StringIO()
        sled.run_b24(plan, confirm=confirm, only={1, 2}, out=out)
        text = out.getvalue()
        assert FAKE_KEY not in text and "osk77" not in text and "/rest/" not in text


def test_state_prevents_double_send(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    state = tmp_path / "plan.sent.json"
    calls: list = []
    monkeypatch.setattr(sled.urllib.request, "urlopen", _ok_urlopen(calls))
    assert sled.run_b24(_plan(), True, {1, 2}, io.StringIO(), state) == 0
    assert len(calls) == 2
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["1"] == {"status": "created", "id": "201"}
    out = io.StringIO()
    assert sled.run_b24(_plan(), True, {1, 2}, out, state) == 0
    assert len(calls) == 2 and "уже создана" in out.getvalue()


def test_unknown_result_blocks_resend(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    state = tmp_path / "plan.sent.json"

    def timeout(req, timeout=0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(sled.urllib.request, "urlopen", timeout)
    out = io.StringIO()
    assert sled.run_b24(_plan(), True, {1}, out, state) == 4
    assert "результат неизвестен" in out.getvalue()
    assert json.loads(state.read_text(encoding="utf-8"))["1"] == {"status": "unknown"}
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    out = io.StringIO()
    assert sled.run_b24(_plan(), True, {1}, out, state) == 6
    assert "Ничего не отправлено" in out.getvalue()


def test_parse_and_ics_commands_never_output_webhook(monkeypatch, tmp_path: Path,
                                                     capsys) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    text = (FIX / "protocol.md").read_text(encoding="utf-8")
    text = text.replace("Планёрка отдела продаж", f"Планёрка {FAKE_WEBHOOK}crm.deal.list.json")
    text = text.replace("Сверка по КП", f"Сверка {FAKE_KEY} https://x.bitrix24.ru/rest/9/k9k9k9/")
    proto = tmp_path / "protocol.md"
    proto.write_text(text, encoding="utf-8")
    assert sled.main(["parse", str(proto), "--tz", "+05:00"]) == 0
    printed = capsys.readouterr().out
    plan = json.loads(printed)  # still valid JSON after redaction
    assert FAKE_KEY not in printed and "k9k9k9" not in printed and "/rest/" not in printed
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    raw_plan = sled.parse_protocol(text, {}, sled.parse_offset("+05:00"))
    pf_raw = tmp_path / "raw.json"  # a plan that still contains the secrets
    pf_raw.write_text(json.dumps(raw_plan, ensure_ascii=False), encoding="utf-8")
    ics = tmp_path / "r.ics"
    assert sled.main(["ics", str(pf_raw), "-o", str(ics)]) == 0
    body = ics.read_bytes().decode("utf-8")
    unfolded = body.replace("\r\n ", "")
    assert FAKE_KEY not in unfolded and "k9k9k9" not in unfolded and "/rest/" not in unfolded
    _check_ics(ics.read_bytes())


def test_unexpected_errors_are_redacted(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)

    def boom(*a, **k):
        raise RuntimeError(f"failed at {FAKE_WEBHOOK}tasks.task.add key {FAKE_KEY}")

    monkeypatch.setattr(sled, "build_ics", boom)
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    assert sled.main(["ics", str(pf)]) == 1
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.err + captured.out and "Ошибка" in captured.err


@pytest.mark.parametrize("response,status", [
    (sled.urllib.error.HTTPError("u", 500, "err", {}, None), "unknown"),
    (sled.urllib.error.HTTPError("u", 400, "bad", {}, None), "failed"),
    ({"result": {}}, "unknown"),
    ({"error": "ERROR_CORE", "error_description": "Не указан исполнитель"}, "failed"),
])
def test_ambiguous_results_stay_unknown(monkeypatch, tmp_path: Path, response, status) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    state = tmp_path / "plan.sent.json"

    def fake(req, timeout=0):
        if isinstance(response, Exception):
            raise response
        return _Resp(json.dumps(response).encode())

    monkeypatch.setattr(sled.urllib.request, "urlopen", fake)
    assert sled.run_b24(_plan(), True, {1}, io.StringIO(), state) == 4
    assert json.loads(state.read_text(encoding="utf-8"))["1"]["status"] == status
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.lock"))
    if status == "unknown":
        monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
        assert sled.run_b24(_plan(), True, {1}, io.StringIO(), state) == 6


def test_parallel_run_is_blocked(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    state = tmp_path / "plan.sent.json"
    with sled.StateLock(state):  # another run holds the lock
        out = io.StringIO()
        assert sled.run_b24(_plan(), True, {1}, out, state) == 7
        assert "Уже идёт другая отправка" in out.getvalue()
    assert not (tmp_path / "plan.sent.json.lock").exists()


def test_argparse_errors_and_saved_path_are_redacted(monkeypatch, tmp_path: Path,
                                                     capsys) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    with pytest.raises(SystemExit):
        sled.main(["b24", "plan.json", FAKE_WEBHOOK, "--bogus", FAKE_KEY])
    captured = capsys.readouterr()
    assert FAKE_KEY not in captured.err + captured.out and "/rest/" not in captured.err
    pf = tmp_path / "plan.json"
    pf.write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    out_file = tmp_path / f"{FAKE_KEY}.ics"
    assert sled.main(["ics", str(pf), "-o", str(out_file)]) == 0
    assert out_file.exists() and FAKE_KEY not in capsys.readouterr().out


@pytest.mark.parametrize("bad_id", [FAKE_WEBHOOK, f"7 {FAKE_KEY}", "0", "-3", "abc", 1.5])
def test_unexpected_task_id_is_unknown(monkeypatch, tmp_path: Path, bad_id) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    state = tmp_path / "plan.sent.json"
    monkeypatch.setattr(sled.urllib.request, "urlopen", lambda req, timeout=0: _Resp(
        json.dumps({"result": {"task": {"id": bad_id}}}).encode()))
    out = io.StringIO()
    assert sled.run_b24(_plan(), True, {1}, out, state) == 4
    saved = state.read_text(encoding="utf-8")
    assert json.loads(saved)["1"] == {"status": "unknown"}
    assert FAKE_KEY not in saved and FAKE_KEY not in out.getvalue()


def test_unknown_result_stops_remaining_tasks(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    state = tmp_path / "plan.sent.json"
    calls = []

    def first_times_out(req, timeout=0):
        calls.append(req)
        raise TimeoutError("timed out")

    monkeypatch.setattr(sled.urllib.request, "urlopen", first_times_out)
    out = io.StringIO()
    assert sled.run_b24(_plan(), True, {1, 2}, out, state) == 4
    assert len(calls) == 1  # task 2 is never sent after an unknown result
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["1"] == {"status": "unknown"} and "2" not in saved
    assert "не отправлены" in out.getvalue()


def test_lock_message_is_redacted(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(sled.WEBHOOK_ENV, FAKE_WEBHOOK)
    monkeypatch.setattr(sled.urllib.request, "urlopen", _no_network)
    state = tmp_path / f"plan-{FAKE_KEY}.sent.json"
    with sled.StateLock(state):
        out = io.StringIO()
        assert sled.run_b24(_plan(), True, {1}, out, state) == 7
    assert FAKE_KEY not in out.getvalue() and "Уже идёт другая отправка" in out.getvalue()
