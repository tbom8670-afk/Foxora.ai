"""Administrative group actions for Soroush Plus.

Lock/unlock policy
------------------
"قفل" and "باز" must toggle *only* the group's ability to post text messages.
Every other default right (media, stickers, gifs, games, inline, embed links,
polls, change info, invite users, pin messages, topics, photos, videos, round
videos, audios, voices, docs) is read from the server and written back
untouched.

"Text messages" means both ``send_messages`` (legacy umbrella flag) and
``send_plain`` (granular text flag). The server can normalise a lock by setting
both, so an unlock that clears only ``send_messages`` leaves ``send_plain``
banned: the RPC succeeds, the bot reports success, and the group stays silent.
Both flags are therefore always written together, and the result is read back
and verified before the operation is reported as successful.

Deliberately NOT used here:

* ``ToggleJoinToSendRequest`` - it only switches the "must join to send" mode.
  Existing members keep writing, so the group never actually locks.
* ``client.edit_permissions()`` - its keyword arguments all default to ``True``
  ("not restricted"), so calling it with a single argument silently clears
  every other restriction the group had configured.

The single correct request is
``messages.EditChatDefaultBannedRightsRequest`` carrying a full
``ChatBannedRights`` object that we cloned from the current server state.

Note on ChatBannedRights semantics: a flag set to ``True`` means the right is
*banned*. So locking is ``send_messages=True`` and unlocking is
``send_messages=False``.
"""
import json
import traceback
from pathlib import Path

from modules.runtime_paths import runtime_config_file
from modules.atomic_write import write_json

try:
    from splusthon.tl import functions, types
    from splusthon.tl.functions.channels import EditPhotoRequest, EditTitleRequest
except ImportError:
    class _Mock:
        pass
    functions = _Mock()
    types = _Mock()
    EditPhotoRequest = _Mock
    EditTitleRequest = _Mock

from modules.group_id import CHANNEL_ID_OFFSET, normalize_group_id


# Every ChatBannedRights flag, in the order declared by the schema. Used to
# clone the server state field-by-field so a library upgrade that adds a new
# flag cannot silently drop it.
BANNED_RIGHT_FLAGS = (
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

# Flags that together decide "can a normal member post a plain text message".
#
# ``send_messages`` is the legacy umbrella flag; ``send_plain`` is the granular
# text-only flag introduced with the granular media rights. The server may
# normalise a ``send_messages`` ban by *also* setting ``send_plain``. If unlock
# clears only ``send_messages`` the leftover ``send_plain`` keeps the group
# silent while the RPC still reports success -- which is exactly the
# "قفل works, باز does not" symptom.
TEXT_MESSAGE_FLAGS = ("send_messages", "send_plain")

_LOCK_STATE_FILE = runtime_config_file("group_message_lock_state.json")


def _load_lock_state():
    try:
        if _LOCK_STATE_FILE.exists():
            return json.loads(_LOCK_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return {}


def _save_lock_state(data):
    try:
        _LOCK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_json(_LOCK_STATE_FILE, data, indent=2)
    except OSError:
        # Persisting the snapshot is best-effort telemetry; never block a lock.
        pass


def clone_banned_rights(rights, **overrides):
    """Return a new ChatBannedRights with every flag copied from ``rights``.

    ``overrides`` replaces individual flags. Passing ``rights=None`` yields an
    all-allowed object, which is the correct representation of "this group has
    no default restrictions yet".
    """
    values = {
        flag: bool(getattr(rights, flag, None)) if rights is not None else False
        for flag in BANNED_RIGHT_FLAGS
    }
    values.update({flag: bool(value) for flag, value in overrides.items()})
    return types.ChatBannedRights(
        until_date=getattr(rights, "until_date", None) if rights is not None else None,
        **values,
    )


def describe_rights(rights):
    """Compact, log-friendly view of the flags that are currently banned."""
    if rights is None:
        return "None"
    banned = [flag for flag in BANNED_RIGHT_FLAGS if getattr(rights, flag, None)]
    return ",".join(banned) if banned else "<no restrictions>"


def rights_to_dict(rights):
    """Serialisable snapshot of every flag, used to restore the pre-lock state."""
    if rights is None:
        return None
    return {flag: bool(getattr(rights, flag, None)) for flag in BANNED_RIGHT_FLAGS}


def text_is_banned(rights):
    """True when a normal member cannot post plain text under ``rights``."""
    if rights is None:
        return False
    return any(bool(getattr(rights, flag, None)) for flag in TEXT_MESSAGE_FLAGS)


class GroupActions:

    def __init__(self, client, logger):
        self.client = client
        self.logger = logger

    @staticmethod
    def _peer_id_candidates(chat_id):
        """Resolve storage's short channel ID to SPlusthon's full -100 ID."""
        try:
            raw_id = int(chat_id)
            short_id = int(normalize_group_id(chat_id))
        except (TypeError, ValueError):
            return (chat_id,)

        # Do not retry the positive short channel ID: SPlusthon interprets it
        # as a user and issues GetUsersRequest (404). A normalized full ID is
        # the only safe numeric lookup; real InputPeers still come from cache.
        if raw_id <= -CHANNEL_ID_OFFSET or raw_id < 0:
            return (raw_id,)
        return (-(CHANNEL_ID_OFFSET + short_id),)

    async def _group_peer(self, chat_id):
        """Prefer the bot's resolved InputPeer, then legacy ID resolution."""
        bot = getattr(self.client, "_outgoing_sender_bot", None)
        cache = getattr(bot, "reply_input_peer_cache", {}) or {}
        wanted = normalize_group_id(chat_id)
        direct = cache.get(chat_id)
        if direct is not None:
            self.logger.log_info(
                f"GROUP PEER CACHE HIT chat_id={chat_id} peer_type={type(direct).__name__}"
            )
            return direct
        for cached_id, peer in list(cache.items()):
            if peer is not None and normalize_group_id(cached_id) == wanted:
                self.logger.log_info(
                    f"GROUP PEER CACHE HIT chat_id={chat_id} "
                    f"cached_id={cached_id} peer_type={type(peer).__name__}"
                )
                return peer

        last_error = None
        for candidate_id in self._peer_id_candidates(chat_id):
            try:
                peer = await self.client.get_input_entity(candidate_id)
                self.logger.log_info(
                    f"GROUP PEER RESOLVED chat_id={chat_id} resolved_chat_id={candidate_id} "
                    f"peer_type={type(peer).__name__}"
                )
                return peer
            except Exception as error:
                last_error = error
                self.logger.log_info(
                    f"GROUP PEER RESOLVE RETRY chat_id={chat_id} candidate_id={candidate_id} "
                    f"error={error!r}"
                )
        raise ValueError(
            f"Group peer could not be resolved for chat_id={chat_id}; "
            f"candidates={self._peer_id_candidates(chat_id)}"
        ) from last_error

    async def _fetch_default_banned_rights(self, chat_id, peer):
        """Read the group's *current* default rights straight from the server.

        ``get_entity()`` may answer from the local session cache, and a stale
        answer would make us write back outdated flags. So the authoritative
        sources are queried first, cache-backed lookups only as a last resort.

        Returns ``(rights, source)``. ``rights`` may be ``None`` when the group
        genuinely has no default restrictions configured.
        """
        attempts = []

        if hasattr(peer, "channel_id") and hasattr(peer, "access_hash"):
            input_channel = types.InputChannel(peer.channel_id, peer.access_hash)

            async def from_full_channel():
                result = await self.client(
                    functions.channels.GetFullChannelRequest(input_channel)
                )
                for chat in getattr(result, "chats", ()) or ():
                    if getattr(chat, "id", None) == peer.channel_id:
                        return chat
                chats = getattr(result, "chats", ()) or ()
                return chats[0] if chats else None

            async def from_get_channels():
                result = await self.client(
                    functions.channels.GetChannelsRequest([input_channel])
                )
                chats = getattr(result, "chats", ()) or ()
                return chats[0] if chats else None

            attempts.append(("channels.GetFullChannel", from_full_channel))
            attempts.append(("channels.GetChannels", from_get_channels))

        elif hasattr(peer, "chat_id"):

            async def from_get_chats():
                result = await self.client(
                    functions.messages.GetChatsRequest([peer.chat_id])
                )
                chats = getattr(result, "chats", ()) or ()
                return chats[0] if chats else None

            attempts.append(("messages.GetChats", from_get_chats))

        async def from_get_entity():
            return await self.client.get_entity(chat_id)

        attempts.append(("get_entity", from_get_entity))

        last_error = None
        for source, operation in attempts:
            try:
                chat = await operation()
            except Exception as error:
                last_error = error
                self.logger.log_info(
                    f"RIGHTS FETCH RETRY chat_id={chat_id} source={source} error={error!r}"
                )
                continue

            if chat is None:
                self.logger.log_info(
                    f"RIGHTS FETCH EMPTY chat_id={chat_id} source={source}"
                )
                continue

            rights = getattr(chat, "default_banned_rights", None)
            self.logger.log_info(
                f"RIGHTS FETCH OK chat_id={chat_id} source={source} "
                f"chat_type={type(chat).__name__} banned=[{describe_rights(rights)}]"
            )
            return rights, source

        raise ValueError(
            f"default_banned_rights could not be read for chat_id={chat_id}"
        ) from last_error

    async def _set_text_messages_banned(self, chat_id, banned, operation):
        """Flip the text-message flags only, then verify the server agreed.

        Both ``send_messages`` and ``send_plain`` are written, because the
        server may normalise one into the other. Every unrelated flag is copied
        from the current server state and written back untouched.

        ``operation`` is "LOCK" or "UNLOCK" and only decorates the log lines.
        """
        peer = await self._group_peer(chat_id)
        current, source = await self._fetch_default_banned_rights(chat_id, peer)

        overrides = {flag: banned for flag in TEXT_MESSAGE_FLAGS}
        updated = clone_banned_rights(current, **overrides)

        changed = [
            flag
            for flag in BANNED_RIGHT_FLAGS
            if bool(getattr(current, flag, None) if current is not None else False)
            != bool(getattr(updated, flag))
        ]
        self.logger.log_info(
            f"{operation} RIGHTS DIFF chat_id={chat_id} source={source} "
            f"before=[{describe_rights(current)}] after=[{describe_rights(updated)}] "
            f"changed={changed or ['<none>']}"
        )

        unexpected = [flag for flag in changed if flag not in TEXT_MESSAGE_FLAGS]
        if unexpected:
            # Defensive guard: never ship a request that touches another right.
            raise ValueError(
                f"Refusing to edit rights for chat_id={chat_id}: "
                f"unexpected changed flags {unexpected}"
            )

        self.logger.log_info(
            f"{operation} RPC SEND chat_id={chat_id} "
            f"banned_rights_sent=[{describe_rights(updated)}]"
        )
        result = await self.client(
            functions.messages.EditChatDefaultBannedRightsRequest(
                peer=peer,
                banned_rights=updated,
            )
        )
        self.logger.log_info(
            f"{operation} RPC ACK chat_id={chat_id} result_type={type(result).__name__}"
        )

        # A successful RPC does not prove the group actually changed: the server
        # may normalise or silently reject flags. Read the state back and check.
        verified, verify_source = await self._fetch_default_banned_rights(chat_id, peer)
        self.logger.log_info(
            f"{operation} RPC VERIFY chat_id={chat_id} source={verify_source} "
            f"sent=[{describe_rights(updated)}] "
            f"server_now=[{describe_rights(verified)}] "
            f"text_banned={text_is_banned(verified)} expected_text_banned={bool(banned)}"
        )

        if text_is_banned(verified) != bool(banned):
            still = [
                flag
                for flag in TEXT_MESSAGE_FLAGS
                if bool(getattr(verified, flag, None)) != bool(banned)
            ]
            self.logger.log_error(
                f"{operation} RPC INEFFECTIVE chat_id={chat_id} "
                f"sent=[{describe_rights(updated)}] "
                f"server_now=[{describe_rights(verified)}] "
                f"mismatched_flags={still}"
            )
            raise ValueError(
                f"{operation} did not take effect for chat_id={chat_id}: "
                f"expected text_banned={bool(banned)}, server reports "
                f"text_banned={text_is_banned(verified)}; mismatched flags {still}. "
                f"Server state: [{describe_rights(verified)}]"
            )

        drifted = [
            flag
            for flag in BANNED_RIGHT_FLAGS
            if flag not in TEXT_MESSAGE_FLAGS
            and bool(getattr(current, flag, None) if current is not None else False)
            != bool(getattr(verified, flag, None) if verified is not None else False)
        ]
        if drifted:
            self.logger.log_error(
                f"{operation} RIGHTS DRIFT chat_id={chat_id} "
                f"unrelated flags changed server-side: {drifted} "
                f"before=[{describe_rights(current)}] after=[{describe_rights(verified)}]"
            )

        return result, current, verified

    async def lock_group(self, chat_id, minutes=None):
        """Ban the default text-message rights so normal members cannot write."""
        self.logger.log_info(f"LOCK RPC START chat_id={chat_id}")
        try:
            _result, previous, _verified = await self._set_text_messages_banned(
                chat_id, True, "LOCK"
            )
        except Exception:
            self.logger.log_error(
                f"LOCK RPC FAILED chat_id={chat_id}\n{traceback.format_exc()}"
            )
            raise

        # Store the complete pre-lock snapshot so unlock can restore exactly
        # what the group had, instead of guessing.
        state = _load_lock_state()
        state[normalize_group_id(chat_id)] = {
            "previous_rights": rights_to_dict(previous),
            "previous_banned_flags": describe_rights(previous),
        }
        _save_lock_state(state)

        self.logger.log_info(f"LOCK RPC SUCCESS chat_id={chat_id}")
        return True

    async def unlock_group(self, chat_id):
        """Clear the default text-message ban and verify the group really opened.

        Both ``send_messages`` and ``send_plain`` are cleared. Any other right
        that the group had before the lock is restored from the saved snapshot,
        so unlocking never grants something the owner had deliberately closed.
        """
        self.logger.log_info(f"UNLOCK RPC START chat_id={chat_id}")

        state = _load_lock_state()
        saved = state.get(normalize_group_id(chat_id)) or {}
        snapshot = saved.get("previous_rights")
        self.logger.log_info(
            f"UNLOCK SNAPSHOT chat_id={chat_id} "
            f"has_snapshot={snapshot is not None} "
            f"previous_banned_flags={saved.get('previous_banned_flags', '<none>')!r}"
        )

        try:
            _result, _current, verified = await self._set_text_messages_banned(
                chat_id, False, "UNLOCK"
            )
        except Exception:
            self.logger.log_error(
                f"UNLOCK RPC FAILED chat_id={chat_id}\n{traceback.format_exc()}"
            )
            raise

        # Restore any non-text right that drifted away from the pre-lock state.
        if snapshot:
            restore = {
                flag: snapshot[flag]
                for flag in BANNED_RIGHT_FLAGS
                if flag not in TEXT_MESSAGE_FLAGS
                and flag in snapshot
                and bool(getattr(verified, flag, None)) != bool(snapshot[flag])
            }
            if restore:
                self.logger.log_info(
                    f"UNLOCK RESTORE chat_id={chat_id} restoring_flags={restore}"
                )
                try:
                    peer = await self._group_peer(chat_id)
                    overrides = dict(restore)
                    overrides.update({flag: False for flag in TEXT_MESSAGE_FLAGS})
                    restored = clone_banned_rights(verified, **overrides)
                    await self.client(
                        functions.messages.EditChatDefaultBannedRightsRequest(
                            peer=peer,
                            banned_rights=restored,
                        )
                    )
                    self.logger.log_info(
                        f"UNLOCK RESTORE OK chat_id={chat_id} "
                        f"rights=[{describe_rights(restored)}]"
                    )
                except Exception:
                    # The group is already open; a failed cosmetic restore must
                    # not turn a successful unlock into an error for the user.
                    self.logger.log_error(
                        f"UNLOCK RESTORE FAILED chat_id={chat_id}\n"
                        f"{traceback.format_exc()}"
                    )

        state = _load_lock_state()
        state.pop(normalize_group_id(chat_id), None)
        _save_lock_state(state)

        self.logger.log_info(f"UNLOCK RPC SUCCESS chat_id={chat_id}")
        return True

    async def change_title(self, chat_id, title):
        chat = await self.client.get_input_entity(chat_id)
        await self.client(EditTitleRequest(chat, title))

    async def change_photo(self, chat_id, file_path):
        chat = await self.client.get_input_entity(chat_id)
        uploaded = await self.client.upload_file(file_path)

        from splusthon.tl.types import InputChatUploadedPhoto

        await self.client(
            EditPhotoRequest(chat, InputChatUploadedPhoto(file=uploaded))
        )
