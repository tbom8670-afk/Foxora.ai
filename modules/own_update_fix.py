"""Stop the library from asking Soroush for "difference" after our own deletes.

Problem (measured with the real ``MessageBox``, see
``tests/test_own_update_fix.py``): after ``channels.DeleteMessagesRequest``
the server answers ``messages.AffectedMessages(pts, pts_count)``.  That pts
belongs to the *group*, but SPlusthon files it under the *account* counter
(``UpdateDeleteMessages`` has no ``channel_id``).  Two things follow:

* the account counter looks like it jumped -> ``updates.GetDifference``;
* the group counter never learns about our delete, so the very next message in
  that group looks like a gap -> ``updates.GetChannelDifference``.

So one delete cost up to three round trips.  The fix keeps the library's own
mechanism but files the pts under the right group, as
``UpdateDeleteChannelMessages(channel_id, [], pts, pts_count)``.  Nothing is
dispatched to handlers (the update stays flagged ``_self_outgoing``), no
request is added or removed, and anything unexpected falls back to the
library's original behaviour.

Disable with ``OWN_DELETE_PTS_FIX=0``.
"""

import datetime
import os

STATS = {"tagged": 0, "channel_pts": 0, "fallback": 0}

_INSTALLED = False
_TAG = "_fx_channel_id"
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
# Channel requests whose answer carries the *channel* pts.
_REQUEST_NAMES = (
    "DeleteMessagesRequest",
    "DeleteTopicHistoryRequest",
    "DeleteUserHistoryRequest",
    "DeleteParticipantHistoryRequest",
)


def enabled():
    value = os.getenv("OWN_DELETE_PTS_FIX", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _channel_id_of(request):
    channel = getattr(request, "channel", None)
    channel_id = getattr(channel, "channel_id", None)
    if isinstance(channel_id, int) and channel_id > 0:
        return channel_id
    return None


def _patch_request_class(cls):
    if cls is None or getattr(cls, "_fx_read_result_patched", False):
        return False
    original = cls.read_result          # staticmethod(reader) in the library

    def read_result(self, reader):
        result = original(reader)
        try:
            channel_id = _channel_id_of(self)
            if channel_id is not None and getattr(result, "pts", None):
                setattr(result, _TAG, channel_id)
                STATS["tagged"] += 1
        except Exception:
            pass
        return result

    cls.read_result = read_result
    cls._fx_read_result_patched = True
    return True


def install():
    """Idempotent, class level (rebuilt clients are covered too)."""
    global _INSTALLED
    if _INSTALLED:
        return True
    if not enabled():
        return False
    try:
        from splusthon.network.mtprotosender import MTProtoSender
        from splusthon.tl import functions, types
    except Exception:
        return False

    affected_ids = frozenset(
        cls.CONSTRUCTOR_ID
        for cls in (
            getattr(types.messages, "AffectedMessages", None),
            getattr(types.messages, "AffectedHistory", None),
        )
        if cls is not None
    )
    update_cls = getattr(types, "UpdateDeleteChannelMessages", None)
    short_cls = getattr(types, "UpdateShort", None)
    original_store = getattr(MTProtoSender, "_store_own_updates", None)
    if not affected_ids or update_cls is None or short_cls is None \
            or original_store is None:
        return False

    def _store_own_updates(self, obj, *args, **kwargs):
        try:
            channel_id = getattr(obj, _TAG, None)
            if (
                channel_id is not None
                and getattr(obj, "CONSTRUCTOR_ID", None) in affected_ids
                and enabled()
            ):
                update = short_cls(
                    update_cls(
                        channel_id, [], obj.pts,
                        getattr(obj, "pts_count", 0) or 0,
                    ),
                    _EPOCH,
                )
                update._self_outgoing = True
                self._updates_queue.put_nowait(update)
                STATS["channel_pts"] += 1
                return None
        except Exception:
            STATS["fallback"] += 1
        return original_store(self, obj, *args, **kwargs)

    patched = 0
    for name in _REQUEST_NAMES:
        if _patch_request_class(getattr(functions.channels, name, None)):
            patched += 1
    if not patched:
        return False
    MTProtoSender._store_own_updates = _store_own_updates
    _INSTALLED = True
    return True
