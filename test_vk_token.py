"""Проверка пользовательского токена ВК.

    python test_vk_token.py            # только проверить токен
    python test_vk_token.py --send 123 # ещё и отправить тестовое сообщение в peer_id=123
"""

from __future__ import annotations

import argparse
import sys

import requests

from core.config import load_config

API_HOST = "https://api.vk.com"
TEST_TEXT = "Тестовое сообщение от помощника учителя. Если вы его видите — токен рабочий."


def call(token: str, version: str, method: str, **params: object) -> dict:
    payload = {"access_token": token, "v": version, **params}
    response = requests.post(f"{API_HOST}/method/{method}", data=payload, timeout=30)
    body = response.json()
    if "error" in body:
        err = body["error"]
        raise RuntimeError(f"{method}: [{err.get('error_code')}] {err.get('error_msg')}")
    return body["response"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка токена ВК")
    parser.add_argument("--send", type=int, metavar="PEER_ID", help="отправить тестовое сообщение")
    args = parser.parse_args()

    config = load_config()
    token = config.vk.user_token
    if not token:
        print("В config.toml не заполнен [vk].user_token")
        return 1

    try:
        users = call(token, config.vk.api_version, "users.get", fields="screen_name")
    except (RuntimeError, requests.RequestException) as err:
        print(f"Токен не работает: {err}")
        return 1

    me = users[0]
    print(f"OK. Токен живой, ты это {me.get('first_name')} {me.get('last_name')} (id {me.get('id')})")

    try:
        peers = call(token, config.vk.api_version, "messages.getConversations", count=100)
        titles = [f"{p['peer_id']} — {p.get('chat_settings', {}).get('title', '?')}" for p in peers.get("items", [])]
    except (RuntimeError, requests.RequestException) as err:
        titles = [f"<не удалось получить список бесед: {err}>"]

    if titles:
        print("\nБеседы (peer_id — название):")
        print("\n".join(f"  {line}" for line in titles))
        print("\nИх впиши в config.toml как [[chats]] с peer_id.")

    if args.send:
        try:
            result = call(token, config.vk.api_version, "messages.send", peer_id=args.send, message=TEST_TEXT)
        except (RuntimeError, requests.RequestException) as err:
            print(f"\nОтправка не удалась: {err}")
            return 1
        print(f"\nOK. Отправлено в {args.send}, conversation_message_id={result.get('conversation_message_id')}")
    else:
        print("\nЧтобы проверить отправку: python test_vk_token.py --send <peer_id>")

    return 0


if __name__ == "__main__":
    sys.exit(main())