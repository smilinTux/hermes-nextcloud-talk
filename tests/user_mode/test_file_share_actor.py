"""v1.15.1: a file share is not an @mention of its sender, and a room-only
grant counts everywhere the adapter filters senders.

Live 2026-11-14 (Workshop room): Manny (``manolo``, allowed in that room only)
shared a .md file with a caption. Talk puts an ``actor`` rich parameter
(``type: user``, ``id: manolo``) on every file share, so the share looked like
"Manny mentions Manny": the free-room gate filed it as "for someone else",
context only. The context backlog then checked the sender without the room
token, so the room-only grant did not count and the line was thrown away.
Result: no turn, no context, nothing logged.
"""

import asyncio

from user import access
from user.adapter import ROOM_TYPE_GROUP, NextcloudTalkUserAdapter, _mentions_someone_else
from gateway.config import PlatformConfig

ROOM = "u7o3gwt7"
ACCESS = f"""
users: [dknestri]
rooms:
  {ROOM}: {{users: [manolo], only: false}}
"""


def _run(coro):
    return asyncio.run(coro)


def _file_param(name, file_id):
    return {"type": "file", "id": file_id, "name": name, "size": "9362",
            "path": f"Talk/Workshop-{ROOM}/Manolo-manolo/{name}", "mimetype": "text/markdown",
            "permissions": "1", "link": f"https://cloud.example.com/f/{file_id}"}


ACTOR = {"type": "user", "id": "manolo", "name": "Manolo", "mention-id": "manolo"}


def _share(msg_id, text, name, file_id):
    # Shape of the real messages 15624 / 15625 (Talk 22, nor), trimmed.
    return {"id": msg_id, "token": ROOM, "actorType": "users", "actorId": "manolo",
            "actorDisplayName": "Manolo", "timestamp": 1794709000, "message": text,
            "messageType": "comment", "systemMessage": "", "isThread": None, "threadId": msg_id,
            "messageParameters": {"actor": ACTOR, "file": _file_param(name, file_id)}}


def _adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_ALLOWED_USERS", "")
    monkeypatch.setenv("NEXTCLOUD_TALK_ALLOW_ALL_USERS", "")
    p = tmp_path / "talk-access.yaml"
    p.write_text(ACCESS)
    monkeypatch.setenv(access.ACCESS_FILE_ENV, str(p))
    a = NextcloudTalkUserAdapter(PlatformConfig(extra={
        "username": "lumina", "display_name": "Lumina", "room_modes": f"{ROOM}=free"}))
    a._room_meta[ROOM] = {"token": ROOM, "type": ROOM_TYPE_GROUP,
                          "displayName": "Chef · Te Ha Keh · Lumina: Workshop"}

    async def _no_download(path):
        return b"# Office product overview\n"
    a._download_own_file = _no_download
    return a


def test_file_share_actor_param_is_not_a_mention_of_someone_else():
    params = {"actor": ACTOR, "file": _file_param("x.md", "1")}
    assert not _mentions_someone_else("Hello to both.", params, "lumina", "Lumina", frozenset())
    assert not _mentions_someone_else("{file}", params, "lumina", "Lumina", frozenset())


def test_real_mention_of_someone_else_still_counts():
    params = {"mention-user1": {"type": "user", "id": "dknestri", "name": "Chef"}}
    assert _mentions_someone_else("{mention-user1} can you look?", params, "lumina", "Lumina", frozenset())


def test_room_guest_file_share_with_caption_takes_a_turn(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _run(a._on_message(ROOM, _share(15625, "Hello to both. Lumina now you can get a good picture.",
                                    "OFFICE-PRODUCT-OVERVIEW.md", "20229876")))
    assert len(a.handled_events) == 1
    ev = a.handled_events[0]
    assert ev.source.user_id == "manolo"
    assert "Hello to both" in ev.text
    assert "OFFICE-PRODUCT-OVERVIEW.md" in ev.text
    assert ev.reply_expected is False  # unaddressed in a free room: the agent may stay quiet


def test_room_guest_bare_file_share_takes_a_turn(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    _run(a._on_message(ROOM, _share(15624, "{file}", "DOORWAY-PRODUCT-OVERVIEW.md", "20229877")))
    assert len(a.handled_events) == 1
    assert "DOORWAY-PRODUCT-OVERVIEW.md" in a.handled_events[0].text


def test_room_guest_line_kept_as_context_for_the_next_turn(tmp_path, monkeypatch):
    a = _adapter(tmp_path, monkeypatch)
    # Manny addresses Chef only: context, no turn. It must survive for Chef's next turn.
    params = {"mention-user1": {"type": "user", "id": "dknestri", "name": "Chef"}}
    msg = {"id": 1, "actorType": "users", "actorId": "manolo", "actorDisplayName": "Manolo",
           "message": "{mention-user1} did you see my doc?", "messageType": "comment",
           "messageParameters": params, "timestamp": 1794709000}
    _run(a._on_message(ROOM, msg))
    assert a.handled_events == []
    assert any("did you see my doc?" in line for line in a._room_context.get(ROOM, []))
