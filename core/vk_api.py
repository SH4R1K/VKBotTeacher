from __future__ import annotations

import asyncio
import logging
import random
import ssl
from functools import cache
from typing import Any

import aiohttp

try:
    import certifi
except ImportError:  # certifi необязателен: без него работает системное хранилище
    certifi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

API_HOST = "https://api.vk.com"
DEFAULT_API_VERSION = "5.199"
CHAT_TYPE = 2
CONVERSATION_COUNT = 100
TIMEOUT_SECONDS = 30
CHAT_SCAN_LIMIT = 300
CHAT_SCAN_CONCURRENCY = 10
CHAT_SCAN_MISSES = 250
SCAN_TIMEOUT_SECONDS = 120

COMMUNITY_MESSAGES_HINT = (
    "у ключа нет права messages. Пересоздай ключ: Управление сообществом → "
    "Дополнительно → Работа с API → отметь «Разрешение на отправку сообщений»"
)
USER_MESSAGES_HINT = (
    "у ключа нет права messages. Пользовательским ключам оно выдаётся только "
    "по заявке в devsupport@corp.vk.com"
)
TLS_HINT = (
    "не удалось подтвердить сертификат ВК: в системе нет корня SSL.com, "
    "которым подписан pu.vk.com. Поставь пакет certifi: pip install certifi"
)
MULTICHAT_MIN_PEER_ID = 2_000_000_001

PHOTOS_SCOPE_HINT = (
    "у ключа нет доступа к фотографиям. Пересоздай ключ сообщества: Управление "
    "сообществом → Дополнительно → Работа с API → отметь «Доступ к фотографиям» "
    "вместе с «Разрешение на отправку сообщений». Пока галочка не стоит, любой "
    "способ загрузки файла отклоняется с кодом 15, а отправка готового "
    "вложения — нет: сначала нужен адрес загрузки"
)

ADD_COMMUNITY_HINT = (
    "добавь сообщество в беседу: Управление сообществом → Сообщения → Настройки для бота → "
    "включи «Возможности ботов» и «Разрешать добавлять сообщество в беседы», затем в меню "
    "сообщества нажми «Пригласить в беседу». Если сообщество уже стоит в участниках, но было "
    "добавлено обычным приглашением — это не бот: убери его из беседы и добавь заново кнопкой"
)

VK_HINTS = {
    5: "токен недействителен — пересоздай ключ сообщества.",
    15: "у ключа нет права messages. Пересоздай ключ: Управление сообществом → Дополнительно → "
    "Работа с API → отметь «Разрешение на отправку сообщений».",
    900: "пользователь заблокировал сообщество.",
    901: "пользователь не разрешил сообществу писать ему (нет диалога или запрет в настройках).",
    912: "включи «Возможности ботов» в настройках сообщества.",
    917: "у сообщества нет доступа к беседе по API. " + ADD_COMMUNITY_HINT + ".",
    925: "сообщество не администратор беседы.",
    927: "такой беседы не существует — перепроверь peer_id.",
    932: "сообщество не может взаимодействовать с этим получателем.",
    945: "беседа отключена.",
    983: "на запись в эту беседу стоит ограничение.",
    984: "на сообщество стоит ограничение на запись из-за жалоб на спам.",
    1012: "в этой беседе запрещено писать.",
}


def explain(err: VKError) -> str:
    if err.code == 15 and err.method.split(".")[0] in ("photos", "docs"):
        return f"{PHOTOS_SCOPE_HINT} ({err})"
    hint = VK_HINTS.get(err.code)
    return f"{hint} ({err})" if hint else str(err)


def suspicious_peer_id(peer_id: int | None) -> bool:
    return peer_id is not None and 0 < peer_id < MULTICHAT_MIN_PEER_ID


class VKError(RuntimeError):
    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method}: [{code}] {message}")
        self.code = code
        self.method = method


@cache
def vk_ssl_context() -> ssl.SSLContext:
    """Контекст TLS: системное хранилище Windows плюс bundle certifi.

    Нужен для pu.vk.com — домена загрузки фото. ВК отдаёт для него цепочку
    «*.vk.com → SSL.com TLS Issuing RSA CA R1 → SSL.com TLS RSA Root CA 2022 →
    SSL.com EV Root Certification Authority RSA R2», а последнего корня нет
    в хранилище Windows, из-за чего Python отвергает всю цепочку с
    «self-signed certificate in certificate chain».
    """
    context = ssl.create_default_context()
    if certifi is None:
        return context
    try:
        context.load_verify_locations(cafile=certifi.where())
    except (OSError, ssl.SSLError) as err:
        log.warning("Не читается bundle certifi (%s), остаюсь на системных корнях", err)
    return context


def vk_session(timeout: float = TIMEOUT_SECONDS) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout),
        connector=aiohttp.TCPConnector(ssl=vk_ssl_context()),
    )


async def vk_call(
    session: aiohttp.ClientSession,
    method: str,
    token: str,
    api_version: str,
    **params: Any,
) -> dict:
    payload = {**params, "access_token": token, "v": api_version}
    async with session.post(f"{API_HOST}/method/{method}", data=payload) as resp:
        body = await resp.json(content_type=None)
    if "error" in body:
        err = body["error"]
        raise VKError(method, err.get("error_code", 0), err.get("error_msg", "неизвестная ошибка"))
    return body.get("response", {})


def _group_items(response: object) -> list[dict[str, Any]]:
    """groups.getById отдаёт {"groups": [...]}, а не список — приводим к списку."""
    if isinstance(response, dict):
        response = response.get("groups", [])
    if not isinstance(response, list):
        return []
    return [item for item in response if isinstance(item, dict)]


def _is_chat(peer: dict[str, Any]) -> bool:
    # ВК 5.199 отдаёт тип строкой внутри conversation.peer, в старых версиях — числом на верхнем уровне.
    return peer.get("type") == CHAT_TYPE or peer.get("type") == "chat"


def _peer_of(item: dict[str, Any]) -> dict[str, Any]:
    conversation = item.get("conversation")
    if isinstance(conversation, dict) and isinstance(conversation.get("peer"), dict):
        return conversation["peer"]
    # messages.getConversationsById кладёт peer сразу в элемент, без обёртки conversation
    peer = item.get("peer")
    return peer if isinstance(peer, dict) else {}


def _chats_from(response: object) -> list[dict[str, Any]]:
    items = response.get("items", []) if isinstance(response, dict) else []
    chats: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        conversation = item.get("conversation")
        container = conversation if isinstance(conversation, dict) else item
        peer = _peer_of(item)
        if peer:
            if not _is_chat(peer):
                continue
            peer_id = peer.get("id")
        elif item.get("type") != CHAT_TYPE:
            continue
        else:
            peer_id = item.get("peer_id")
        if not peer_id:
            continue
        settings = container.get("chat_settings") or {}
        chats.append({"peer_id": peer_id, "title": settings.get("title") or f"Беседа {peer_id}"})
    return chats


async def _with_messages_info(
    session: aiohttp.ClientSession,
    token: str,
    api_version: str,
    info: dict[str, Any],
    hint: str,
) -> dict[str, Any]:
    """Дополняет результат проверки информацией о праве messages."""
    try:
        response = await vk_call(
            session,
            "messages.getConversations",
            token,
            api_version,
            count=CONVERSATION_COUNT,
        )
    except VKError as err:
        info["messages_ok"] = False
        info["messages_error"] = str(err)
        info["hint"] = hint if err.code == 15 else None
    else:
        info["messages_ok"] = True
        info["chats_count"] = len(_chats_from(response))
    return info


async def check_community_token(
    token: str, api_version: str = DEFAULT_API_VERSION
) -> dict[str, Any]:
    if not token:
        raise VKError("groups.getById", 0, "Токен сообщества не задан")
    async with vk_session() as session:
        response = await vk_call(
            session, "groups.getById", token, api_version, fields="screen_name"
        )
        groups = _group_items(response)
        if not groups:
            raise VKError("groups.getById", 0, "токен не принадлежит сообществу")
        group = groups[0]
        info: dict[str, Any] = {
            "kind": "community",
            "id": group.get("id"),
            "name": group.get("name") or "без названия",
        }
        return await _with_messages_info(
            session, token, api_version, info, COMMUNITY_MESSAGES_HINT
        )


async def check_token(
    token: str, api_version: str = DEFAULT_API_VERSION, mode: str = "user_token"
) -> dict[str, Any]:
    if mode == "community":
        return await check_community_token(token, api_version)
    if not token:
        raise VKError("users.get", 0, "Токен не задан")
    async with vk_session() as session:
        response = await vk_call(
            session, "users.get", token, api_version, fields="screen_name"
        )
        items = response if isinstance(response, list) else [response]
        if not items or not isinstance(items[0], dict):
            raise VKError(
                "users.get", 0, "профиль не вернулся — это похоже на токен сообщества"
            )
        user = items[0]
        name = user.get("screen_name") or " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        info: dict[str, Any] = {
            "kind": "user",
            "id": user.get("id"),
            "name": name or "без имени",
        }
        return await _with_messages_info(
            session, token, api_version, info, USER_MESSAGES_HINT
        )


async def _probe_chat(
    session: aiohttp.ClientSession, token: str, api_version: str, peer_id: int
) -> dict[str, Any] | None:
    """Одна беседа по peer_id. None — сообщество в ней не состоит.

    Батчить peer_ids нельзя: один недоступный идентификатор убивает весь вызов
    (927 на всю пачку), поэтому перебор строго по одному peer_id.
    """
    try:
        response = await vk_call(
            session, "messages.getConversationsById", token, api_version, peer_ids=peer_id
        )
    except VKError as err:
        log.debug("peer_id %s: %s", peer_id, err)
        return None
    return next((chat for chat in _chats_from(response)), None)


async def scan_chats(
    token: str,
    api_version: str = DEFAULT_API_VERSION,
    *,
    limit: int = CHAT_SCAN_LIMIT,
    concurrency: int = CHAT_SCAN_CONCURRENCY,
    stop_after_misses: int = CHAT_SCAN_MISSES,
) -> list[dict[str, Any]]:
    """Перебирает peer_id мультичатов: 2000000001, 2000000002, ...

    Нужен потому, что messages.getConversations показывает сообществу не все
    беседы, в которых оно состоит. Возвращает найденные по порядку peer_id.
    """
    if not token:
        raise VKError("messages.getConversationsById", 0, "Токен не задан")
    found: list[dict[str, Any]] = []
    misses = 0
    async with vk_session(timeout=SCAN_TIMEOUT_SECONDS) as session:
        for start in range(1, limit + 1, concurrency):
            if stop_after_misses and misses >= stop_after_misses:
                log.info(
                    "Перебор остановлен на local_id %s: %s пропусков подряд",
                    start - 1,
                    misses,
                )
                break
            window = range(start, min(start + concurrency, limit + 1))
            local_ids = list(window)
            peers = [
                MULTICHAT_MIN_PEER_ID - 1 + local_id for local_id in local_ids
            ]
            log.info("Перебор бесед: local_id %s-%s", local_ids[0], local_ids[-1])
            results = await asyncio.gather(
                *(
                    _probe_chat(session, token, api_version, peer)
                    for peer in peers
                )
            )
            for local_id, chat in zip(local_ids, results, strict=True):
                if chat is None:
                    misses += 1
                    continue
                misses = 0
                found.append(chat | {"local_id": local_id})
    return found


async def fetch_conversations(
    token: str,
    api_version: str = DEFAULT_API_VERSION,
    count: int = CONVERSATION_COUNT,
    *,
    scan: bool = True,
    scan_limit: int = CHAT_SCAN_LIMIT,
) -> list[dict[str, Any]]:
    """Беседы из списка диалогов плюс найденные перебором peer_id."""
    if not token:
        raise VKError("messages.getConversations", 0, "Токен не задан")
    async with vk_session() as session:
        try:
            response = await vk_call(
                session, "messages.getConversations", token, api_version, count=count
            )
        except VKError as err:
            log.info("Список диалогов недоступен (%s), беру только перебор", err)
            response = {}
        chats = _chats_from(response)
    if scan:
        chats.extend(await scan_chats(token, api_version, limit=scan_limit))
    merged: dict[int, dict[str, Any]] = {}
    for chat in chats:
        merged.setdefault(chat["peer_id"], chat)
    return sorted(merged.values(), key=lambda chat: chat["peer_id"])


async def _probe(
    session: aiohttp.ClientSession,
    token: str,
    api_version: str,
    title: str,
    method: str,
    **params: Any,
) -> dict[str, Any]:
    try:
        response = await vk_call(session, method, token, api_version, **params)
    except VKError as err:
        return {"title": title, "ok": False, "code": err.code, "text": explain(err)}
    return {"title": title, "ok": True, "response": response}


def _conversation_ids(response: object) -> set[int]:
    if not isinstance(response, dict):
        return set()
    ids: set[int] = set()
    for item in response.get("items", []) or []:
        peer = (item.get("conversation") or {}).get("peer") if isinstance(item, dict) else None
        if isinstance(peer, dict) and isinstance(peer.get("id"), int):
            ids.add(peer["id"])
    return ids


def _same_owner(left: object, right: object) -> bool:
    """Сверяет id сообщества: в groups.getById он положительный, в составе беседы — отрицательный."""
    try:
        return abs(int(left)) == abs(int(right))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False


TEST_MESSAGE = (
    "Тестовая отправка от помощника учителя. Если вы это видите — доступ к беседе работает."
)


async def test_send(
    token: str,
    peer_id: int,
    api_version: str = DEFAULT_API_VERSION,
    text: str = TEST_MESSAGE,
) -> dict[str, Any]:
    """Единственная проверка, которая ничего не доказывает на словах: реальная отправка."""
    if peer_id < MULTICHAT_MIN_PEER_ID:
        return {
            "ok": False,
            "error": (
                f"peer_id {peer_id} меньше {MULTICHAT_MIN_PEER_ID} — это не беседа, "
                "а пользователь. Возьми id беседы из ссылки vk.com/im?sel=2000000000."
            ),
        }
    async with vk_session(timeout=60) as session:
        message_id = await vk_call(
            session, "messages.send", token, api_version,
            peer_id=peer_id, message=text, random_id=random.randint(1, 2**31 - 1),
        )
    return {"ok": True, "peer_id": peer_id, "message_id": message_id, "text": text}


async def diagnose_chat(
    token: str,
    peer_id: int,
    api_version: str = DEFAULT_API_VERSION,
    mode: str = "community",
) -> dict[str, Any]:
    """Проверяет, имеет ли сообщество доступ к беседе, не отправляя в неё ничего.

    ВК игнорирует peer_ids в messages.getConversations и возвращает все диалоги,
    поэтому «беседа есть в списке» проверяется по составу ответа, а не по коду.
    Для сообщества messages.getHistory бесполезен: по документации чтение истории
    беседы от имени сообщества недоступно и всегда даёт 15.
    """
    if peer_id < MULTICHAT_MIN_PEER_ID:
        return {
            "ok": False,
            "error": (
                f"peer_id {peer_id} меньше {MULTICHAT_MIN_PEER_ID} — это не беседа, "
                "а пользователь. Возьми id беседы из ссылки vk.com/im?sel=2000000000."
            ),
        }
    if not token:
        return {"ok": False, "error": "Токен не задан"}

    who: dict[str, Any] = {}
    kind = "user" if mode == "user_token" else "community"
    identity_method = "users.get" if kind == "user" else "groups.getById"
    async with vk_session() as session:
        identity = await _probe(session, token, api_version, "Кто я", identity_method, fields="screen_name")
        if identity["ok"]:
            groups = _group_items(identity["response"])
            first = groups[0] if groups else None
            name = ""
            if isinstance(first, dict):
                if kind == "community":
                    name = first.get("name") or ""
                else:
                    name = first.get("screen_name") or " ".join(
                        part
                        for part in (first.get("first_name"), first.get("last_name"))
                        if part
                    )
            who = {
                "kind": kind,
                "id": first.get("id") if isinstance(first, dict) else None,
                "name": name or "без имени",
            }
        listed_check = await _probe(
            session, token, api_version, "Беседа есть в списке диалогов сообщества",
            "messages.getConversations", peer_ids=peer_id, count=100,
        )
        if listed_check["ok"] and peer_id not in _conversation_ids(listed_check["response"]):
            listed_check = {
                "title": listed_check["title"],
                "ok": False,
                "code": 0,
                "text": "беседы нет среди диалогов сообщества — оно не состоит в ней",
            }
        checks = [listed_check]
        if kind == "user":
            checks.append(
                await _probe(
                    session, token, api_version, "Есть доступ ко всей переписке беседы",
                    "messages.getHistory", peer_id=peer_id, count=1,
                )
            )
        checks.append(
            await _probe(
                session, token, api_version,
                "Виден состав беседы (нужны права администратора беседы)",
                "messages.getConversationMembers", peer_id=peer_id,
            )
        )

    members_check = checks[-1]
    members: list[Any] = []
    if members_check["ok"]:
        raw = members_check["response"]
        members = raw.get("items", []) if isinstance(raw, dict) else []
        who["in_members"] = any(
            isinstance(m, dict) and _same_owner(m.get("member_id", m.get("id")), who.get("id"))
            for m in members
        )
    for check in checks:
        check.pop("response", None)

    listed = checks[0]["ok"]
    readable = checks[1]["ok"] if kind == "user" else None
    members_visible = members_check["ok"]
    if listed and members_visible:
        verdict = "Сообщество — администратор беседы. Отправка точно проходит."
    elif listed:
        verdict = (
            "Беседа есть в списке диалогов сообщества, писать оно может. Состав беседы "
            "не виден — права администратора не выданы. Нажми «Тестовая отправка», чтобы "
            "проверить отправку по-настоящему."
        )
    else:
        verdict = (
            "Беседы нет среди диалогов сообщества — оно не состоит в ней по API, даже если "
            "стоит в списке участников. " + ADD_COMMUNITY_HINT
        )
    return {
        "ok": True,
        "peer_id": peer_id,
        "who": who,
        "checks": checks,
        "access": {
            "listed": listed,
            "readable": readable,
            "members_visible": members_visible,
        },
        "verdict": verdict,
    }