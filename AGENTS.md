# TohaVibeCode

Локальный помощник учителя: веб-приложение на aiohttp, которое разбирает методички
PDF, генерирует по ним сообщения через OpenAI-совместимый ИИ и рассылает результат
в беседы ВК (или кладёт в буфер обмена).

## Commands
- Запуск: `python app.py` — сам поднимает сервер из `[server]` и открывает браузер.
- Зависимости: `pip install -r requirements.txt`.
- Линт: `python -m ruff check .` (конфига нет, ruff берёт дефолты).
- Синтаксис фронта: `node --check web\app.js`.
- Ручная проверка токена: `python test_vk_token.py [--send PEER_ID]`.
- Тестового фреймворка в проекте нет — см. Tests.

## Architecture
- `app.py` — единственная точка входа. aiohttp-роуты, `Context` в `app["ctx"]`,
  вся сборка ответов JSON. Не ходит в VK и ИИ сам, только зовёт `core/` и `sender/`.
- `core/` — логика без знания о вебе: `config.py` (pydantic-модели),
  `config_store.py` (запись TOML), `pdf_parser.py`, `prompt_builder.py`,
  `llm.py`, `vk_api.py`, `history.py`, `thumbs.py`, `naming.py`.
- `sender/` — единственное место, где живёт VK-токен при отправке: `VKSender`,
  `ClipboardSender`, сборка `Sender` через `build_sender` (`sender/__init__.py:31`).
- `web/` — ванильный JS без сборки. Состояние в `state`, API через `api()/post()`.
- Настройки живут в `config.toml`, но правятся из веба: `POST /api/settings`
  → `core/config_store.save_config` → `Context.apply` (`app.py:76`) пересоздаёт
  клиента и отправителя, перезапуск не нужен.

## Conventions
- Тексты ошибок и подсказок — по-русски, внятно; технические детали (код ВК,
  имя файла) добавляются в конце.
- Любая работа с файлами и `pypdf` — через `asyncio.to_thread` (`app.py:127`).
- Путь к файлу из запроса собирать только через `Context.lesson/photo/history` —
  это `_resolve()` с `Path(name).name`, единственная защита от `..`
  (`app.py:82`). Собирать `dir / request.query[...]` руками нельзя.
- Адреса ИИ-провайдеров живут только в `LLM_PROVIDERS` (`core/config.py:12`);
  `base_url` в UI — лишь подстановка пресета.
- `config_store.render_config` пишет TOML сам и проверяет round-trip через
  `AppConfig.model_validate(tomllib.loads(text))`. Добавляя поле в
  `core/config.py`, допиши его и в `render_config`, иначе настройка не переживёт
  сохранение.
- Новый роут — в список `app.router.add_routes` (`app.py:662`), возвращать
  `_json(...)`, исключения ловит middleware `errors` (`app.py:133`).

## Tests
- Фреймворка нет. Проверки пишутся как одноразовые скрипты в
  `%TEMP%\opencode` (вне репозитория), гоняются через `aiohttp.test_utils`
  `TestServer` на реальном `create_app()` и удаляются после прогона.
- Проверять надо в первую очередь: отказ вместо 500 на кривых данных,
  сохранение `config.toml` (sha256 до/после), и что `_resolve` не пускает `..`.
- Ловушки: порядок истории при равных mtime недетерминирован (нужен `os.utime`);
  в PowerShell без `$env:PYTHONIOENCODING="utf-8"` ломается кириллица;
  битое фото `/api/photo/thumb` отдаёт оригинал, а не ошибку.

## Forbidden
- Не читать, не печатать и не коммитить токены (`[vk]` в `config.toml`) —
  только программно через `load_config()`.
- `config.toml.bak` — автобэкап с секретами, руками не трогать и в репозиторий
  не тащить.
- `photos/.thumbs/` — кэш миниатюр, генерируется сам, удаляется вместе с фото.
- `.ruff_cache/`, `__pycache__/`, `photos/`, `history/` — не в репозиторий.

## Context
- `peer_id` мультичата индивидуален для каждого участника: у человека один,
  у сообщества другой. Поэтому ID беседы из ссылки пользователя не подходит
  боту, и беседы приходится находить перебором `peer_id`
  (`core/vk_api.py`, `scan_chats`) и мержить с `messages.getConversations`.
- `messages.getConversationsById` нельзя батчить: один недоступный `peer_id`
  роняет всю пачку (927).
- Отправка фото требует у community-ключа галочки «доступ к фотографиям»;
  без неё `photos.getMessagesUploadServer` даёт код 15, и это не лечится кодом.