# След — встреча → протокол → задачи

После встречи пишешь «разбери встречу» — и получаешь протокол: решения, задачи с ответственными
и сроками, открытые вопросы. Задачи ставятся в Битрикс24 или Google Календарь.

## Как поставить

### Claude Code

```
/plugin marketplace add DianaMarshrut/labops-sled
/plugin install sled@labopsai-sled
```

### Codex

```
codex plugin marketplace add DianaMarshrut/labops-sled
```

Затем `/plugins` → Install → начни новую сессию.

### Claude (claude.ai и приложение)

1. «Customize» → «Plugins» → «Add marketplace» → вставь `DianaMarshrut/labops-sled` → «Install».
2. Создай проект «Встречи», подключи коннекторы Granola и Google Calendar
   («Customize» → «Connectors») и напиши в проекте: «настрой след».

Альтернатива — загрузить ZIP вручную:
1. Скачай `dist/sled.zip`. Распаковывать не нужно.
2. «Customize» → «Skills» → «+» → «+ Create skill» → «Upload a skill» → выбери `sled.zip`.

### ChatGPT

Создай проект «Встречи», добавь в файлы проекта `chat-instruction.md`.
Напиши в проекте: «Работай по инструкции из файлов проекта».

## Откуда берём встречу

1. **Текст или файл расшифровки** — вставь в чат. Любой сервис: Телемост, Zoom, Контур.Толк.
2. **Granola** — через коннектор.
   Гайд: https://guides.labopsai.pro/granola-claude
3. **Нет расшифровки** — протокол из заметок.
   Гайд: https://guides.labopsai.pro/meeting-notes-auto

## Куда ставим задачи

- **Битрикс24** — через MCP. Гайд: https://guides.labopsai.pro/bitrix24-claude
- **Google Календарь** — через коннектор Google Calendar.
- **Нет подключения** — готовый список для ручного ввода.

## Ограничения

- На встрече не присутствует — работает только с расшифровкой или заметками.
- Без подключения задачи нужно завести вручную.

## Команды

- «Вот расшифровка, сделай протокол»
- «Поставь задачи в Битрикс24»
- «Отметь маркетинг как срочное»

## Безопасность

- Задачи и события создаются только после «да» на точный список.
- Bitrix24: используй MCP-подключение. Ключи в чат не вставляй.

## Что в репозитории

| Путь | Для чего |
|---|---|
| `plugins/sled/skills/sled/` | Навык: `SKILL.md`, справочник `references/sources.md`, шаблон протокола |
| `dist/sled.zip` | Навык в архиве — для загрузки в Claude |
| `chat-instruction.md` | Навык одним текстом — для ChatGPT и Codex |
| `scripts/build.py` | Пересобирает `dist/sled.zip` и `chat-instruction.md` после правок |

## Для разработчика

Нужен Python 3.10+.

```
python3 scripts/build.py
```

## Другие капсулы labopsai

- [Пульс — дашборд руководителя](https://github.com/DianaMarshrut/labops-puls)
- [Радар — утренняя сводка](https://github.com/DianaMarshrut/labops-radar)
