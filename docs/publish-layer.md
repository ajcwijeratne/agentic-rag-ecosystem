# Publish layer

Phase 2 turns the production `publish` state into a governed channel action.
No channel delivery is attempted until the production is in `publish` or
`measure` and every gate from the `review -> publish` boundary is approved.
The delivery boundary rechecks those gates on every attempt.

## Publication targets

Set `publish_targets` when creating or updating a production:

```json
[
  {"channel": "linkedin"},
  {"channel": "youtube", "options": {"privacy_status": "private"}}
]
```

When an approved production advances into `publish`, declared targets run once.
Publication records are unique by production and channel, so retries cannot
create duplicate YouTube uploads. A channel can also be invoked explicitly with
`POST /production/{production_id}/publish`.

## LinkedIn

LinkedIn is deliberately a human handoff. The service sends the final copy and
asset IDs to the configured notification channels and records the publication as
`handoff_ready`. After pasting the post, call
`POST /publications/{publication_id}/confirm` with the public URL. The record
then becomes `published` and receives `published_at`.

## YouTube

YouTube uses the official Data API v3 resumable upload flow. Configure:

```dotenv
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
YOUTUBE_PRIVACY_STATUS=private
```

The uploader selects a readable linked video asset. Uploads default to private;
set `privacy_status` to `unlisted` or `public` in the target options only when
that is intentional. OAuth credentials and access tokens are never stored in
publication metadata.

## API

- `GET /publications`
- `GET /publications/{publication_id}`
- `POST /production/{production_id}/publish`
- `POST /publications/{publication_id}/confirm`

Publication rows contain production ID, channel, status, URL, external ID,
timestamps, error text, and non-secret channel metadata. Phase 3 outcomes use
the publication ID as their foreign key.
