# Nextcloud tools for the agent (optional, MCP)

This plugin makes the agent a participant in Talk. To let it also *use*
Nextcloud (read files, calendars, contacts, Deck, Tables, Mail, Cookbook,
Collectives, News...), add a Nextcloud MCP server to Hermes. We evaluated the
options (2026-10-25) and recommend
[cbcoutinho/nextcloud-mcp-server](https://github.com/cbcoutinho/nextcloud-mcp-server)
(AGPL-3.0, separate install, not bundled here): verified live with Hermes over
stdio, 174 tools discovered.

## Config

Add to `config.yaml`. It reuses this plugin's server URL and user, so no
secret is written into the config; a **separate app password** for the MCP
server is recommended so it can be revoked on its own (Settings > Security >
Devices & sessions):

```yaml
mcp_servers:
  nextcloud:
    command: uvx                      # uv must be on the gateway service's PATH
    args: ["nextcloud-mcp-server==0.201.2", "run", "--transport", "stdio", "--log-level", "warning"]
    env:
      NEXTCLOUD_HOST: "${NEXTCLOUD_TALK_SERVER_URL}"
      NEXTCLOUD_USERNAME: "${NEXTCLOUD_TALK_USERNAME}"
      NEXTCLOUD_PASSWORD: "${NEXTCLOUD_MCP_APP_PASSWORD}"   # separate app password recommended; ${NEXTCLOUD_TALK_APP_PASSWORD} also works
    connect_timeout: 120              # first uvx run downloads about 220 MB
    idle_timeout_seconds: 900
    tools:
      include: [ ...the read-only list below... ]
      resources: false
      prompts: false
```

`uv` must be on the gateway service's `PATH`. Pin the version: upstream
releases about daily, and re-check the allowlist when you bump it.

## Start read-only

Talk is a prompt channel: anyone in a room the agent answers in (including
federated users) can try to talk it into using its tools. The full server has
about 99 write tools (21 marked destructive: deleting files, sending mail,
creating public links). Start with this read-only allowlist, taken from the
server's own `readOnlyHint` markings on v0.201.2:

```yaml
include: [nc_tables_list_tables, nc_tables_get_schema, nc_tables_read_table,
  nc_webdav_list_directory, nc_webdav_read_file, nc_webdav_search_files, nc_webdav_find_by_name,
  nc_webdav_find_by_type, nc_webdav_list_favorites, nc_webdav_list_comments, nc_webdav_list_trash,
  nc_webdav_list_versions, nc_webdav_list_tags, nc_webdav_get_file_tags, nc_webdav_find_by_tag_name,
  nc_share_get, nc_share_list, nc_calendar_list_calendars, nc_calendar_list_events, nc_calendar_get_event,
  nc_calendar_get_upcoming_events, nc_calendar_find_availability, nc_calendar_list_todos,
  nc_calendar_search_todos, collectives_get_collectives, collectives_get_pages, collectives_get_page,
  collectives_search_pages, collectives_get_tags, collectives_get_trashed_pages,
  collectives_get_trashed_collectives, nc_contacts_list_addressbooks, nc_contacts_list_contacts,
  nc_contacts_search_contacts, nc_cookbook_list_recipes, nc_cookbook_get_recipe, nc_cookbook_search_recipes,
  nc_cookbook_list_categories, nc_cookbook_get_recipes_in_category, nc_cookbook_list_keywords,
  nc_cookbook_get_recipes_with_keywords, deck_get_boards, deck_get_board, deck_get_stacks, deck_get_stack,
  deck_get_archived_stacks, deck_get_cards, deck_get_board_overview, deck_get_card, deck_get_labels,
  deck_get_label, deck_get_card_comments, deck_list_attachments, nc_news_list_folders, nc_news_list_feeds,
  nc_news_list_items, nc_news_get_item, nc_news_get_starred_items, nc_news_get_unread_items,
  nc_news_get_feed_health, nc_news_get_status, nc_mail_list_accounts, nc_mail_list_mailboxes,
  nc_mail_list_messages, nc_mail_get_message, nc_mail_get_attachment, talk_list_conversations,
  talk_get_conversation, talk_get_messages, talk_list_participants, talk_list_reactions,
  nc_shopping_list_get_lists, nc_shopping_list_get_list, nc_shopping_list_get_items,
  nc_notes_search_notes, nc_notes_get_note, nc_notes_get_attachment]
```

Notes:

- Do not build the list from name patterns: `*_list_*` also matches write
  tools such as `nc_shopping_list_delete_list`.
- Even if you enable write tools later, keep the MCP server's Talk write tools
  (`talk_send_message`, `talk_create_conversation`, `talk_add_participant`,
  `talk_react`) off. They would bypass this plugin's room modes, allowlist and
  agent-to-agent loop guard.
- The real least-privilege boundary is the agent's own account: the MCP server
  sees only what is shared with it.
- To expose the tools on Talk only, list `nextcloud` in
  `platform_toolsets.nctalk` (a listed name is an allowlist, so name every MCP
  server you want there).
- Known upstream issue (v0.201.2): `talk_list_conversations` fails on
  federated rooms.
- First access to Deck creates Deck's own "Welcome" board on the account.
