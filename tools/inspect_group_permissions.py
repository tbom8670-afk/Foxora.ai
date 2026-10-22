"""Read-only Soroush Plus group-permission inspector.

Usage:
    python tools/inspect_group_permissions.py -1000023164149

It sends no message and performs no edit/permission-changing request.
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bot_working_split_ok import SoroushAntiSpamBot
from splusthon.tl import functions, types

RIGHT_FIELDS = (
    "view_messages",
    "send_messages",
    "send_media",
    "send_stickers",
    "send_gifs",
    "send_games",
    "send_inline",
    "embed_links",
    "send_polls",
    "change_info",
    "invite_users",
    "pin_messages",
    "manage_topics",
    "send_photos",
    "send_videos",
    "send_roundvideos",
    "send_audios",
    "send_voices",
    "send_docs",
    "send_plain",
)


def show_rights(label, rights):
    print(f"\n=== {label} ===")
    if rights is None:
        print("None")
        return
    print("type:", type(rights).__name__)
    for field in RIGHT_FIELDS:
        if hasattr(rights, field):
            print(f"{field}={getattr(rights, field)!r}")


def show_chat(label, chat):
    print(f"\n=== {label} ===")
    if chat is None:
        print("None")
        return
    print("type:", type(chat).__name__)
    for field in ("id", "title", "megagroup", "broadcast", "creator", "admin_rights"):
        if hasattr(chat, field):
            print(f"{field}={getattr(chat, field)!r}")
    show_rights("default_banned_rights", getattr(chat, "default_banned_rights", None))
    show_rights("banned_rights", getattr(chat, "banned_rights", None))


async def try_read(label, operation):
    print(f"\n--- {label} ---")
    try:
        result = await operation()
        print("status: OK")
        return result
    except Exception as error:
        print("status: FAILED")
        print("exception_type:", type(error).__name__)
        print("exception:", repr(error))
        return None


async def inspect_group(chat_id):
    bot = SoroushAntiSpamBot()
    client = await bot.initialize_client()
    try:
        await client.connect()
        print("connected: True")

        peer = await try_read("get_input_entity", lambda: client.get_input_entity(chat_id))
        if peer is not None:
            print("peer_type:", type(peer).__name__)
            print("peer:", peer)

        entity = await try_read("get_entity", lambda: client.get_entity(chat_id))
        show_chat("get_entity result", entity)

        if peer is not None and hasattr(peer, "channel_id") and hasattr(peer, "access_hash"):
            input_channel = types.InputChannel(peer.channel_id, peer.access_hash)
            channels = await try_read(
                "channels.GetChannelsRequest",
                lambda: client(functions.channels.GetChannelsRequest([input_channel])),
            )
            if channels is not None:
                print("channels_result_type:", type(channels).__name__)
                print("chats_count:", len(getattr(channels, "chats", ())))
                for index, chat in enumerate(getattr(channels, "chats", ()), 1):
                    show_chat(f"GetChannels chat #{index}", chat)

            full = await try_read(
                "channels.GetFullChannelRequest",
                lambda: client(functions.channels.GetFullChannelRequest(input_channel)),
            )
            if full is not None:
                print("full_result_type:", type(full).__name__)
                full_chat = getattr(full, "full_chat", None)
                print("full_chat_type:", type(full_chat).__name__ if full_chat else None)
                if full_chat is not None:
                    for field in ("id", "about", "participants_count", "admins_count", "banned_count"):
                        if hasattr(full_chat, field):
                            print(f"full_chat.{field}={getattr(full_chat, field)!r}")
                print("full.chats_count:", len(getattr(full, "chats", ())))
                for index, chat in enumerate(getattr(full, "chats", ()), 1):
                    show_chat(f"GetFullChannel chat #{index}", chat)

        if peer is not None and hasattr(peer, "chat_id"):
            chats = await try_read(
                "messages.GetChatsRequest",
                lambda: client(functions.messages.GetChatsRequest([peer.chat_id])),
            )
            if chats is not None:
                print("chats_result_type:", type(chats).__name__)
                print("chats_count:", len(getattr(chats, "chats", ())))
                for index, chat in enumerate(getattr(chats, "chats", ()), 1):
                    show_chat(f"GetChats chat #{index}", chat)
    finally:
        if client.is_connected():
            await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Read-only group permission inspector")
    parser.add_argument("chat_id", type=int)
    args = parser.parse_args()
    asyncio.run(inspect_group(args.chat_id))


if __name__ == "__main__":
    main()
