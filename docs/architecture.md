# Architecture

## Overview

Stash is an application for saving and searching personal content.

The MVP supports:
- User registration and authentication with username/email and password.
- Saving text.
- Uploading images.
- Uploading any file (max 500 MB) as a `file` item. Recognized formats (PDF,
  Office, ODF, iWork, EPUB, FB2, MOBI, DjVu, text/data, common video and
  audio...) keep their MIME type; other files are stored as generic downloads (see
  `backend/api/src/app/items/files.py`). `analyzable` formats get an
  automatic description of what the document is and is about.
- Automatic image description and tag generation.
- Semantic and keyword-based search across saved content.
- Searching by tags and generated descriptions.

Search is hybrid: literal matches (filenames, the user's own text, full-text
over descriptions) plus semantic ones, where each item's text is split into
short chunks embedded with OpenAI embeddings, and an item matches a query by
its closest chunk (pgvector).

## Components

### API Service

Responsible for:
- Authentication and user management.
- Creating and retrieving items.
- Authorizing image/file uploads, which clients send straight to object
  storage (see "Uploads"), and creating their items.
- Search.
- Sending content processing jobs to the queue.

### Processing Worker

Processes saved content asynchronously. Lives in `backend/workers/`.

Package layout: each worker is its own package, with its own
`pyproject.toml`, dependencies, settings, Dockerfile and tests:
`thumbnailer`, `image_analyzer`, `document_analyzer`, `embedding_worker`
(each in `backend/workers/<worker>/`, named like its compose service). What
they all build on (the `Worker`, the status/completion SQL, S3 store,
OpenAI client setup, settings base, both runtimes, test fakes) is a library
package, `stash-worker-core` (`backend/workers/core/`, import
`stash_worker_core`). Workers depend on the core and `stash-shared`, never
on each other: an SQL statement or helper only one worker needs lives in
that worker. Each image contains only its worker, the core and
`stash-shared`, so a change to one worker rebuilds and redeploys only that
worker; a change to the core or `stash-shared` rebuilds them all. Workers
still interact at runtime through queue payloads (`ProcessingJob`) and the
database schema, so a change there must stay compatible with both producer
and consumer. The core is kept separate from `stash-shared` because the API
depends on `stash-shared` and has no use for worker code.

Responsibilities:
- Generate image descriptions.
- Generate tags.
- Generate embeddings.
- Store processing results in PostgreSQL.

Current implementation (MVP): only images are processed (text/link items are
stored already `completed`). Images go through a two-stage pipeline, each
stage a separate worker with its own package (see
"Package layout" above):

1. Thumbnail worker (`thumbnailer` service, consumes `thumbnail_jobs`):
   downloads the original (the whole file: decoding needs all of it; at
   most the 100 MB upload limit) to an anonymous temporary file in `/tmp`,
   in 8 MB ranges, never whole into memory, makes a WebP thumbnail with Pillow (max
   1024px on the longest side, EXIF orientation applied), stores it at
   `users/{user_id}/thumbnails/{item_id}.webp`, records it in
   `item_images.thumbnail_key` (only if the item is still that user's),
   and only then publishes to `content_analysis_jobs`, pointing that job at
   the thumbnail. Undecodable uploads fail here (only PNG, JPEG, GIF and
   WebP decoders ever run), and so do images over a limit, all checked from
   the header before decoding: 50,000 px on either side
   (`THUMBNAIL_MAX_WIDTH`/`_HEIGHT`), 250 MP as stored
   (`THUMBNAIL_MAX_DECLARED_PIXELS`, also Pillow's own decompression-bomb
   guard, whose warning is an error) and 50 MP as decoded
   (`THUMBNAIL_MAX_PIXELS`, 200 MB of pixels): a JPEG decodes straight at
   1/2-1/8 scale (never below the thumbnail size), so a 200 MP photo costs
   a few megapixels, while a small file declaring huge dimensions (a
   decompression bomb) is refused unread. Only the first frame of an
   animation is decoded.
2. Image-analyzer worker (`image_analyzer` service, consumes
   `content_analysis_jobs`): sends the thumbnail — not the original — to the
   OpenAI Responses API (`OPENAI_API_KEY`, model via `OPENAI_MODEL`), which
   answers with a JSON list of short search chunks (structured output), each
   one searchable concept ("anime woman, girl, female", "cyberpunk city,
   futuristic urban environment", legible text as written...). It stores them
   one per line in `item_descriptions` and completes the item, handing it on
   to the embedding stage (see "Search"). Tag generation isn't implemented
   yet.

What's sent to OpenAI is the user's content (a thumbnail, a document's text
and filename, search chunks, search queries), so every Responses API call
(image and document descriptions, search query normalization) sets
`store=False`: OpenAI keeps no stored response to retrieve later, and
nothing here reads one back. The Embeddings API has no such option (it
stores no responses). Both remain under OpenAI's own API data retention
(abuse monitoring: up to 30 days), which only an account-level agreement
(Zero Data Retention) changes, not a request parameter. See "Data
retention and privacy".

The item is `processing` across both stages. Clients display the thumbnail
(`thumbnail_url`), falling back to the original until it exists. Both
stages share the same `Worker` (status handling, retries, dead-lettering,
acking) with a stage-specific handler, and each stage is idempotent, so a
redelivered job — including a duplicate hand-off between stages — never
causes duplicate or incorrect state. Each delivery is processed by
`Worker.process_message`, whatever runtime received it:

- Locally: Valkey → `Worker.run_forever` (a long-running consumer loop) →
  `process_message`.
- AWS Lambda: SQS → event source mapping → the stage's
  `<worker>.aws_lambda.handler` → `process_message` for each
  record of the batch, in order. See "Lambda runtime" under Queue.

Each stage's worker is built once, in its `stage.build_worker`, for both.

Failure handling:
- Each attempt is one queue delivery; nothing is retried in-process, and
  the attempt number is the queue's own delivery count (Valkey's, or SQS's
  `ApproximateReceiveCount`), never an application counter.
- Transient errors (OpenAI 429/5xx, timeouts, network, storage outages) are
  released unacked for redelivery; the item stays `processing` in between.
  When they come back is the queue's `RetryMode` (`stash_shared.queue.base`):
  - Valkey (`backoff`): after the worker's exponential backoff + jitter
    (`RETRY_BASE_DELAY_SECONDS`/`RETRY_MAX_DELAY_SECONDS`).
  - SQS (`visibility_timeout`): once the message's visibility timeout
    expires. The worker computes no delay, and nothing is deleted,
    re-sent or re-timed; retry timing is the queue's configuration.
- Permanent errors (OpenAI rejecting the input as invalid, missing object in
  storage, malformed job) skip the remaining attempts: the item fails at
  once. On SQS the message still goes round until redrive moves it to the
  DLQ, but those redeliveries find the item `failed` and don't re-run the
  handler.
- After 5 deliveries, or on a permanent error, the job is sent to the
  dead-letter queue and the item is marked `failed`.
- A message is acked only after its outcome is durable (description +
  `completed` committed in one transaction, or dead-lettered + `failed`).
  All status writes are guarded by the current status, so redeliveries and
  duplicates are safe no-ops.
- A worker crash mid-job is covered by the queue: the unacked message is
  redelivered after the visibility timeout.
- A job can't be lost between a database commit and its publish: every job
  goes through the transactional outbox (see "Outbox" under Queue), in the
  same transaction as the change that needs it. A stage's hand-off to the
  next is part of its durable outcome in the same way.
- Not covered: a message the queue itself loses after accepting it (Valkey
  losing un-persisted writes, so run it with AOF persistence), and an item
  whose last delivery SQS moves to its DLQ before the worker's last
  attempt (keep `maxReceiveCount` = `MAX_DELIVERY_ATTEMPTS`). Both leave
  the item `pending`/`processing`; dead-lettered jobs are replayed from the
  DLQ. `items.status_updated_at` (stamped on every status write and at the
  start of every processing attempt) shows how long an item has been stuck.

### PostgreSQL

Primary database.

Stores:
- Users.
- Items.
- Text content.
- Image metadata.
- Descriptions — one per item, the single text source search reads from:
  AI-generated for images (by the worker), the item's own text for
  notes/links (written by the API on save).
- Tags and collections.
- Search chunks — each description split into short pieces, each with its
  embedding (`item_search_chunks`).
- Processing status.
- Pending object deletions (`storage_deletions`) and where the orphan
  scan resumes (`storage_reconciliation`); see "Deleting stored objects".

The schema is managed with Alembic (`backend/api/alembic`). By default the
API container applies pending migrations on startup. Setting
`RUN_MIGRATIONS=false` turns that off, for deployments where the API's
database user has DML rights only; migrations then run as a separate
one-off task from the same image (`alembic upgrade head`), as the schema
owner, before the API rolls out.

On AWS, RDS is private, so migrations run in the VPC, in a dedicated
Lambda (`app.aws_lambda_migrations.handler`, packaged with the API's code
and `alembic/`). Nothing triggers it. The deployment invokes it
synchronously after `terraform apply` and before the frontend, and fails
if it fails (see [deployment.md](deployment.md#migrations)). The API
Lambda never migrates.

Database roles (AWS; `app.db_roles`). Only migrations connect as the
schema owner (RDS's master user). The API connects as `stash_api` (rows of
every application table, not `alembic_version`) and every worker as
`stash_worker`: only the tables processing touches, and for updates only
the columns it writes (`items.status`/`status_updated_at`,
`item_images.thumbnail_key`, descriptions, search chunks, the outbox). The
worker role can't read users, tokens, pending uploads, tags or rate-limit
counters, or change an item's owner, type or storage keys. Neither owns
anything or has any role attribute, so neither can alter the schema or
roles. The migration Lambda creates both after every `alembic upgrade
head` and replaces their privileges with what `app.db_roles` lists, so a
worker SQL change that needs another table or column updates
`WORKER_PRIVILEGES` in the same change. Each login has its own secret, and
each function's role reads only its own (the master's only migrations).
Row ownership (`items.user_id`) is still enforced by the application; there
is no row-level security. Locally every service connects as the owner.

### Object Storage

Stores uploaded images.

The application uses an S3-compatible API through `boto3`.

Environments:
- Local: MinIO.
- Production: Amazon S3.

The same storage integration should work in both environments; endpoint, credentials, bucket and other configuration are environment-specific.
An empty endpoint means AWS S3 itself, and empty access keys mean boto3's
default credential chain (e.g. an ECS task role), so the same code also runs
on AWS without static keys.

Key layout (`stash_shared.storage_keys`, used by the API and the thumbnail
worker), in two disjoint areas:

- `uploads/{user_id}/{upload_id}`: staging. The only place a browser can
  write (a pre-signed, create-only PUT), never an item's content, and
  expired by a lifecycle rule after a day (see "Uploads").
- `users/{user_id}/images/{item_id}/{object_id}{ext}`: an image item's
  original, canonical and immutable: created once, by the API's copy at
  finalize, and never written again.
- `users/{user_id}/files/{item_id}/{object_id}[{ext}]`: a file item's
  original, likewise.
- `users/{user_id}/thumbnails/{item_id}.webp`: image thumbnail (written by
  the thumbnail worker; re-running it overwrites the same key).

`{object_id}` is random (`new_object_id`, 128 bits), new for every copy: a
canonical key names one stored object, it doesn't address content
(identical content uploaded twice gets two keys), and nothing relies on it
being unguessable. Keys are made from ids, a validated extension and that
id only, never from the uploaded filename. That name is kept in `item_files.filename` /
`item_images.filename`, and downloads are served under it (images uploaded
before their name was kept have none, and are served unnamed). An item's key is stored on its row
(`item_images.storage_key` / `thumbnail_key`, `item_files.storage_key`),
always read back from there, never rebuilt or taken from a queue job, with
two facts about its original's canonical object:

- `content_etag`: its ETag, an object-state token for consistency (workers
  read the object only while it still has it, `If-Match`). Not a content
  identity: S3's ETag is the content's MD5 only for single-part uploads
  without SSE-KMS (not for multipart uploads, whose ETag is
  `<md5 of the parts' md5s>-<part count>`), and even then MD5 isn't
  collision-resistant. Nothing derives a guarantee from what an ETag is.
- `content_sha256`: the hex SHA-256 of the canonical object's bytes,
  computed by S3 itself as it wrote the copy: the content's identity.

So items stored under older layouts keep working: the unscoped one
(`images/…`, `files/…`, `thumbnails/…`) and the one before canonical copies
(`users/{user_id}/images|files/{item_id}{ext}`, the key the upload URL
itself wrote). Those "legacy" items have neither; see "Uploads".

Access: the bucket is private, and clients never get credentials or
direct bucket access. The only way to an object is a short-lived pre-signed
URL: GET for downloads (`IMAGE_DOWNLOAD_URL_TTL_SECONDS`), PUT for uploads
(`UPLOAD_URL_TTL_SECONDS`, see "Uploads"). The API issues a download URL
only for keys of items it loaded filtered by the authenticated user's id, so
someone else's item is indistinguishable from a missing one (404), and
never for a staging key; and an upload URL only for a staging key it just
generated for the requesting user. The bucket policy backs both up
whatever signs the URL: a pre-signed request (`s3:authType` =
`REST-QUERY-STRING`) can't write outside `uploads/`, or read inside it. Deletes likewise take their keys from the owned item's row, or, for a
deleted account, are the user's own prefixes (`users/{user_id}/`,
`uploads/{user_id}/`; `storage_keys.user_prefixes`, the only prefixes
`app.storage.deletions` accepts), or are keys the reconciliation scan
listed and found no row referencing (see "Deleting stored objects"). Clients
can never send a key: the upload and edit requests reject unknown fields.
The user id in a key is for organizing the bucket (per-user cleanup,
lifecycle rules, usage), not authorization. Ownership in PostgreSQL is the
only source of truth. Workers read the object an item's row names, and
only for a job whose `user_id` and `item_type` are the item's (checked
before anything else, see "Queue trust"); the thumbnail worker builds its
thumbnail's key from that verified `user_id`.

Playback: a video or audio file item is played in the clients' own item
viewer with the browser's native `<video>`/`<audio>` player, straight from
storage (ranged GETs, so seeking works). The original upload is played as
is: nothing is transcoded, and a format the browser can't play shows a
message, with the original still downloadable. A listing's `download_url`
lasts an hour and is signed for downloading, so the viewer instead asks
`GET /items/{id}/playback-url` for a fresh URL each time it opens, signed for
`PLAYBACK_URL_TTL_SECONDS` (4 hours by default), owner-only like
every other URL. A URL can still stop working early, when the temporary
credentials that signed it expire, so the client treats an error on a URL
that already played as expiry: it fetches a new one and continues from the
same position.

Expired listing URLs: a tab can stay open for days, far longer than a
listing's URLs last, so the clients refresh an item's URLs themselves
(`ui/src/url_refresh.rs`), with `GET /items/{id}`, which signs new ones
like a listing does. The TTL stays an hour; nothing depends on it being
short or long. Nothing refreshes ahead of time: only media failing to load
starts a refresh.

- Media (a card's thumbnail, a video card's thumbnail or first frame, an
  audio card's duration probe, the viewer's full-size image) reports
  loading and failing to its card, which owns the item's URLs. A failure
  makes the card fetch the item again; the new URL replaces the failed
  one (and a missing one, e.g. a thumbnail made since) in the card and its
  viewer, until the next listing brings its own. URLs that work are kept,
  so they don't load again. The request runs in the card's scope, so
  closing the viewer doesn't cancel it.
- A failure starts a cycle: a refresh right away; if that request fails
  with a network or server error (or 429), a retry 5 s after it finished,
  and if that fails too, one more 20 s after that one finished. Then the
  cycle stops, with the failure shown. A 404 (the item was deleted) stops
  refreshing it. There are no other timers.
- Permanent failures (the object is gone, a format that can't be shown)
  can't loop: a URL that failed never starts a cycle again and isn't
  loaded again; a URL a refresh fetched that fails within 5 minutes can't
  have expired (URLs last an hour), so it doesn't start one either; and
  there's one cycle at a time per item. A listed URL failing, or a fetched
  one failing later (it expired), starts a new cycle, so an hourly expiry
  is refreshed right away however long the tab has been open.
- At most 4 automatic requests are in flight at once across the UI; the
  rest queue, so scrolling a grid of expired thumbnails doesn't send a
  burst.
- Media that can't be loaded never hides its item. An image shows "Couldn't
  load" with a Retry button in its place (on its card and in the viewer),
  an empty box while new URLs are on their way; a video card's thumbnail
  likewise. A video card's first frame or an audio card's duration that
  can't be read is just left out, as for a format the browser can't
  decode. Retry fetches new URLs right away, bypassing the retry delays
  and the queue; success replaces all the item's URLs and ends any cycle
  waiting to retry.
- The players fetch their own URLs (see "Playback"), and show a failure
  with Retry when playback can't recover. Their backstop of 10 refreshes
  in a row starts over whenever a URL loads, or on Retry.
- Opening a file (clicking a non-media file's card, or the file in a
  video, audio or file item's viewer) always fetches the item for a fresh
  URL rather than following the listed one, apart from all of the above.
  On the web the new tab is opened within the click and pointed at the URL
  once it arrives, or the browser would block it as a pop-up; desktop and
  mobile hand the URL to the system browser. If the URL can't be fetched
  (or the tab is blocked), the tab closes and a toast says so.
- The viewer's neighbour preloads use the listed URLs; one that has
  expired just fails, and the image refreshes once it's shown.

Behind `app.storage.base.ObjectStorage` (the S3 implementation,
`S3Storage`, serves MinIO and AWS S3 alike); item logic never calls
boto3 or knows which backend it's on.

### Uploads

Image and file bytes never pass through the API (or its Lambda): the client
uploads them straight to object storage.

    client → POST /uploads {type, filename, content_type, size_bytes, text, tags}
           ← {upload_id, upload: {url, method: PUT, headers}, expires_at}
    client → PUT upload.url (the bytes, with upload.headers) → S3 / MinIO
    client → POST /uploads/{upload_id}/finalize
           ← {id: upload_id, status}   (item created, processing started)

The bytes go to S3 in one pre-signed single `PutObject` (500 MB at most,
well under S3's 5 GB single-PUT limit). S3 Multipart Upload
(CreateMultipartUpload / UploadPart / CompleteMultipartUpload) isn't used
anywhere. Nor is HTTP `multipart/form-data`, which is unrelated to it: the
API's upload requests are JSON metadata only, and the bytes never pass
through the API.

Start (`ItemService.start_upload`): the API validates the declared metadata
first — size (images ≤ 100 MB, files ≤ 500 MB, not empty; a size over the
limit is a 413), an image's type (PNG/JPEG/GIF/WebP), caption and tags — and
issues nothing if it's invalid. It then charges the upload to the user's
quotas (uploads, bytes, and an AI analysis for images and analyzable
formats; see "Rate limits and quotas"): a 429, and nothing issued, if it
doesn't fit. It then generates the item id and a staging key for it
(`uploads/{user_id}/{upload_id}`: never an item's key). The upload URL is
signed for that key, that content type, that exact `Content-Length` and
`If-None-Match: *`, so S3 rejects any other key, type or size (the size
limit is enforced by S3 itself, not only by the API), and any PUT once an
object is there: the URL can create the staging object once, never replace
it. Last, it records a pending upload (`pending_uploads`: id = the future
item id, user, staging key, signed type and size, filename, caption, tag
names, `expires_at`), in the same transaction deleting up to 100 of the
user's abandoned pending rows (see "Incomplete uploads").

Finalize (`ItemService.finalize_upload`): loads the pending upload by id
*and* the authenticated user's id, row-locked (another user's is a 404),
and holds that lock until the item is committed. An upload abandoned
(`expires_at` more than a day past) is discarded instead, a 404. It reads
back what arrived: its size and ETag (HEAD) and first 8 KB (a ranged GET
with `If-Match` on that ETag, so the size and the bytes describe the same
state of the object), never the whole object. The content is validated as it always was:
size, and the format sniffed from those bytes (an image must be the declared
type; a file is classified by extension + content, falling back to generic).
If nothing has arrived yet it's a 409 and the upload stays pending. Invalid
content discards the upload (row and staging object) with a 422; no
canonical object is ever made for it.

Valid content is then made immutable, in three steps:

1. Copy: S3 copies the staging object server-side to a new canonical key
   (random, see "Object Storage") with `CopyObject`:
   `CopySourceIfMatch` = the validated ETag (only the object state that was
   checked), `If-None-Match: *` (never over an existing object),
   `ChecksumAlgorithm: SHA256` (S3 computes the copy's SHA-256 from the
   bytes it writes; a `CopyObject` result is a single-part object, so it's
   a full-object checksum), stored with the validated content type. If the
   staging object was replaced after the check, the copy is refused.
2. Check the copy: a HEAD (`ChecksumMode: ENABLED`) and 8 KB ranged GET of
   the canonical object. Its size and first bytes must be exactly the ones
   validated (all the validation looks at) and its ETag the copy's. This is
   what makes the validation hold for the item without trusting the ETag
   as a content hash: content swapped in under the same ETag (an MD5
   collision, or any store whose ETags don't hash content) that differs in
   what was validated is caught here, on an object nothing else can write.
   A mismatch deletes the copy (never referenced) and is a 409.
3. Record: the SHA-256 is read from that HEAD (`ChecksumSHA256` with
   `ChecksumType` `FULL_OBJECT` or absent; a composite, multipart checksum,
   `<base64>-<parts>`, is not the content's digest and is never used). No
   full-object SHA-256 means the finalize fails (a 500, copy deleted)
   rather than create an item without one.

A 409 creates nothing and leaves the upload pending: the next finalize
validates what's actually there. Then, in one transaction: the item is
created with the upload's id, the canonical key, `content_etag` and
`content_sha256`, its tags linked, its jobs added to the outbox exactly as
before direct uploads (thumbnail / document analysis / embedding), and the
pending row deleted. After the commit the staging object is deleted (best
effort) and the outbox flushed. So the bytes an item has are the canonical
object's, whose size and first bytes are the ones validated and whose
SHA-256 is recorded, from its creation on. Downloads are still served with
the validated type from the row (`ResponseContentType`), not the object's
own: legacy objects were stored with the type expected from the extension.

Cost: all server-side. Finalize makes one `CopyObject` (S3 reads and hashes
the object; up to 500 MB, typically seconds, within the request) plus a
HEAD and an 8 KB GET of the copy; no byte of the object passes through the
API and nothing is held in memory. Hashing in the API instead would mean
downloading up to 500 MB per finalize; a client-computed checksum would
need the frontend to hash the file and S3 to verify it on the PUT.

Concurrency and replays: the pending row's lock serializes finalizes of one
upload; the second waits, then finds the item and returns it. Finalizing
again returns the same item without copying or re-processing it. No job
exists before the commit, so no worker can start on a half-finalized item.
A delete can't race it either: until the commit there's no item to delete.

A finalize that dies after its `CopyObject` succeeded but before its
transaction commits leaves an unreferenced canonical object. A retry never
adopts it (nothing at a key is trusted by name): it copies again to a fresh
key. It's outside `uploads/`, so no lifecycle rule removes it, and it's
only ever reachable by a key no row holds, so it's never served or
processed. The reconciliation scan deletes it once it's a day old (see
"Deleting stored objects"), as it does a copy finalize rejected but
couldn't delete.

After finalize, the upload URL can still create a staging object (the old
one is deleted) until it expires. Nothing ever reads it: the upload is no
longer pending, a replayed finalize returns the item as it is, and it
expires with the lifecycle rule.

Duplicate uploads: an upload that looks like something the user already
has is allowed, and becomes a new item (its own id, `created_at`,
canonical key, and the filename exactly as uploaded, never renamed to
`file (1).pdf`). Filenames are never unique. Clients only warn first, from
metadata alone: before starting any upload they send each staged file's
type (image or file), filename and size in bytes to `POST
/uploads/duplicates` (at most 100 per request), which returns, per file,
the user's items with exactly the same type, filename (as it would be
stored, so the same extension; case-sensitive) and size — how many, first
and most recent `created_at` — from `item_images`/`item_files`
(`filename`, `size_bytes`, indexed together). Nothing is read or hashed,
on the client or the server, so the check is instant for files of any
size; the price is that it's a heuristic: a file with the same name and
size but other content is flagged, a renamed copy isn't, and images saved
before their name was kept never match. Within the batch itself, the
first file with some (type, filename, size) is the primary one and every
later file with the same is a duplicate too (a file both saved and
repeated is one entry, with the saved copies' count and dates). If there
are any duplicates, the client asks per file whether to skip it or upload
another copy (one file: two buttons; several: one row each, with "skip
all" / "upload all copies" that rows can still override); files that are
neither upload as usual and aren't listed. `content_sha256` is still
recorded at finalize, but nothing here uses it.

Queue jobs carry no key (identifiers only, see "Queue trust"): workers
read the item's `storage_key` and `content_etag` from Postgres, joined on
the job's user, and read the original only with `If-Match` on that ETag. Anything else at
the key fails the item permanently, unread. (A consistency check: the
canonical object is never rewritten, so a different ETag there means
something outside the application changed it.)

Incomplete uploads: a started upload that is never finalized (the upload
failed, the tab closed, finalize never arrived) is only a
`pending_uploads` row, maybe with a staging object. It's not an item:
not listed, searched, processed or downloadable, and no job exists for it.
Staging objects are removed by the bucket's lifecycle rule
(`expire-staging-uploads`: everything under `uploads/`, 1 day; locally the
same rule is set by `minio-init`), which can't touch an item's object since
none lives under `uploads/`. The rows are purged by `start_upload`: the
user's rows past `expires_at` by more than `ABANDONED_UPLOAD_GRACE` (1 day;
S3 only checks the URL's expiry when a request starts, so a slow upload may
legitimately finish late) are deleted, `FOR UPDATE SKIP LOCKED` so a
finalize in progress is never raced. Rejected uploads are removed at once.
The API itself only ever deletes staging keys on this path
(`_delete_staging_object` refuses anything else).

Legacy items: items finalized before canonical copies existed reference the
key their upload URL wrote (`users/{user_id}/images|files/{item_id}{ext}`)
and have no `content_etag` or `content_sha256`. They keep working
unchanged: listed, downloaded and processed from that key, read unpinned.
They can no longer be overwritten by any upload URL (the bucket policy
refuses pre-signed writes outside `uploads/`, including URLs issued before
it), but what's there is whatever was last written before that, which may
not be what was validated if an item's URL was reused within its 15
minutes. Neither is backfilled, since the current content isn't known to
be the validated content.

If S3 Multipart Upload is ever added (e.g. for files over 5 GB, or
resumable uploads), the staging → canonical design stays; what must
change:
- Every pre-signed `UploadPart` URL must be for the upload's own staging key
  and multipart upload id (issued by the API, like the PUT URL), and
  `CompleteMultipartUpload` must be done or checked by the API with
  `If-None-Match: *`, so a completed staging object can't be replaced by
  completing another upload to the same key. Upload ids must be recorded on
  the pending row and never reused after completion.
- The ETag will be `<md5 of parts' md5s>-<parts>`: fine, it's only a token.
  A composite checksum is not a content digest: the SHA-256 must still come
  from the canonical copy (step 3), which is single-part.
- `CopyObject` handles sources up to 5 GB; beyond that the copy needs
  `UploadPartCopy` (a multipart copy) whose checksum is composite again, so
  the full-object SHA-256 would need another source (a full-object
  checksum type on the multipart upload, verified by S3, or hashing).
- The existing `abort-incomplete-multipart-uploads` lifecycle rule (7 days)
  already removes abandoned parts.

Verifying AWS S3 checksums: the local stack runs MinIO, which proves
nothing about AWS. Finalize depends on one S3 behaviour MinIO only
approximates: after `CopyObject` with `ChecksumAlgorithm: SHA256`, a
`HeadObject` with `ChecksumMode: ENABLED` returns `ChecksumSHA256` (a
base64 full-object SHA-256) and `ChecksumType` `FULL_OBJECT` or none.
MinIO returns the checksum on the HEAD (verified) but not in the
`CopyObject` response, and ignores `If-None-Match` on copies (harmless:
the destination key is fresh); the code relies only on the HEAD. If AWS
didn't return it, every finalize would fail with a 500 (no item is ever
created without it). So after the first AWS deployment (and after any
change to the bucket's encryption, e.g. to SSE-KMS), as a manual smoke test:
1. Upload and finalize one image and one file (e.g. a PDF) through the app.
2. `aws s3api head-object --bucket <bucket> --key <storage_key>
   --checksum-mode ENABLED` on each item's `storage_key`: expect
   `ChecksumSHA256` and `ChecksumType: FULL_OBJECT`.
3. Check that base64-decoded value is the row's `content_sha256`, and that
   it equals `sha256sum` of the original file and of the object
   (`aws s3 cp s3://<bucket>/<storage_key> - | sha256sum`).
4. Check the row's `content_etag` is the HEAD's `ETag`.
5. Download both from the app, and check the image gets a thumbnail and the
   file is analyzed (workers read with `If-Match` on `content_etag`): the
   items reach `completed`, with no "is not the validated content" errors
   in the worker logs.
The deploy workflow's smoke tests don't upload anything, so this isn't
covered automatically.

Locally the same flow runs against MinIO: upload URLs are signed for
`S3_PUBLIC_ENDPOINT_URL` (reachable from the browser), and MinIO allows
cross-origin requests by default. On AWS the bucket's CORS rule allows
the CloudFront frontend's origin, plus any `s3_cors_allowed_origins`.

### Queue

Connects the API service and processing worker.

Flow:

API → Queue → Worker

Backed by Valkey (Redis-protocol compatible) Streams with a consumer group,
giving at-least-once delivery: a message stays in the group's pending
entries list, with a delivery counter, until acked. Unacked messages idle
longer than a visibility timeout (a crashed consumer, or a delayed retry) are
reclaimed with XAUTOCLAIM, SQS-style. The API and worker never talk to
Valkey directly — both depend only on the `JobQueue` and `DeadLetterQueue`
interfaces in `backend/shared/`, so the concrete backend can be replaced
without touching either service. Valkey has no native DLQ; each queue's dead
letters go to a stream next to it.

The backend is picked by `stash_shared.queue.factory` from `PLATFORM`
(`QUEUE_PROVIDER` overrides it):
- anything but `aws` (local development included): Valkey Streams, as above.
- `aws`: SQS standard queues (`stash_shared.queue.sqs_queue`). Each queue's
  URL comes from `SQS_QUEUE_URLS` (JSON, queue name → URL); credentials and
  region come from boto3's default chain (the execution role), never from
  settings. `ack` deletes the message by its receipt handle, `retry_later`
  leaves it unacked (retried after its visibility timeout), and the
  delivery count is `ApproximateReceiveCount`. Queue backlog metrics come
  from SQS's own CloudWatch metrics.

Dead-lettering goes through `JobQueue.abandon`, which the worker calls for
every delivery it gives up on (after marking the item `failed`), and for
redeliveries of jobs whose item has already failed:
- Valkey: the dead letter is written to `stash:<name>:dead-letter` first,
  then `abandon` acks the original.
- SQS: nothing is sent (`PlatformDeadLetterQueue`) and `abandon` leaves the
  message unacked: it reappears after its visibility timeout and, once
  received `maxReceiveCount` times, the queue's redrive policy (configured
  in Terraform) moves it to its DLQ. `maxReceiveCount` should equal
  `MAX_DELIVERY_ATTEMPTS` (default 5), so the worker's last attempt (which
  marks the item `failed`) is SQS's last receive. Lower, SQS moves messages
  before the worker's last attempt, leaving their items `processing` (only
  a DLQ replay moves them on); higher, the extra receives are only
  skipped/abandoned on their way to the DLQ. Any message for a `failed`
  item ends up in the DLQ.
  The embedding worker doesn't track item status, so each redelivery of a
  message it gave up on is processed again (dead-lettered straight away
  once past `MAX_DELIVERY_ATTEMPTS`) until SQS moves it.

Lambda runtime (SQS only). Each queue worker has a Lambda handler,
`{thumbnailer,image_analyzer,document_analyzer,embedding_worker}.aws_lambda.handler`,
for a function whose SQS event source mapping has `ReportBatchItemFailures`
on. The handler runs the same worker as locally, settling deliveries on a
`stash_shared.queue.sqs_lambda.LambdaSqsQueue` instead of the queue itself,
and returns the partial batch response (`process_sqs_batch`):
- acked (completed, or skipped) → not reported; Lambda deletes it.
- `retry_later` → reported in `batchItemFailures`, nothing else: SQS
  redelivers it once its visibility timeout expires.
- `abandon` (dead-lettered, or an already-failed item) → reported; SQS
  redrive moves it to the DLQ, as above.
- `process_message` raised, or the record isn't a readable SQS message →
  reported, redelivered after the visibility timeout.
The handler makes no SQS call to settle anything (no delete, visibility
change, re-send or DLQ send), and one
record's failure never fails the others. A record without a `messageId`
fails the invocation (whole batch retried). The worker, engine and event
loop are set up on the first invocation and reused; metrics and spans are
flushed at the end of every invocation, since Lambda freezes the process
after it returns. The function's timeout must cover a whole batch processed
sequentially, and the queue's visibility timeout must exceed the function
timeout.

Outbox. Nothing publishes a job directly after a commit. The API and the
workers add each job to the `outbox_events` table with
`stash_shared.outbox.add_event`, in the same database transaction as the
change that needs it: item creation (thumbnail, document-analysis and
embedding jobs; for uploads, on finalize), an edit of an item's searchable text (embedding), a
recorded thumbnail (content analysis), and an analyzer completing an item
(embedding). The change and its job are committed together or not at all.
After the commit, `OutboxPublisher.flush` publishes every unpublished event,
not only the ones just written, through the same `JobQueue` abstraction
(Valkey or SQS), and sets each event's `published_at` once its queue has
accepted it. The API publishes every queue's events; a worker only those
of the queues it hands jobs on to (the thumbnailer `content_analysis_jobs`,
the analyzers `embedding_jobs`), which are all its role may send to. A
failed publish leaves the event for the next flush. There
is no background flusher: after a crash or a queue outage, an event waits
until the next API request that writes an event, or the next job of a
worker that publishes its queue, triggers a flush. Events keep the trace context they were created in,
so a job published by a later flush still joins the trace that caused it.

Concurrent flushes (API replicas, workers) claim batches with
`FOR UPDATE SKIP LOCKED`, so they don't publish the same event at the
same time. Delivery is still at-least-once: if a process dies after
publishing but before `published_at` is saved, the event is published
again. Every consumer is idempotent (see the worker's failure handling), so
a duplicate is skipped or re-runs harmlessly. Published rows are kept (and
nothing prunes them yet).

Queues (on Valkey: stream key `stash:<name>`, one consumer group each, dead
letters in `stash:<name>:dead-letter`):
- `thumbnail_jobs`: API → thumbnail worker.
- `content_analysis_jobs`: thumbnail worker → image-analyzer worker.
- `document_analysis_jobs`: API → document-analyzer worker.
- `embedding_jobs`: API, content analyzer, document analyzer → embedding
  worker.

Queue trust. A queue message is not a trust boundary. A job
(`ProcessingJob`) is identifiers only: `item_id`, `user_id`, `item_type`,
never a storage key, bucket, filename, content type or processing
parameter (older jobs' `image`/`file` are ignored when decoded). Before
anything else, the `Worker` loads the item and drops the job (acked, the
item untouched: not started, failed or processed) unless the item exists
and is that user's and of that type; a finished item's job is skipped. A
forged or stale job can at most re-run an owner's own item's current
stage. Everything the handler reads (keys, ETags, filenames, text) comes
from the item's rows. On AWS, each queue's resource policy denies sending
to anyone but the functions that publish to it (see the outbox above) and
receiving to anyone but its worker.

## Logging

Every backend service logs through one abstraction, `stash_shared.log`:
`logger = get_logger(__name__)`, then `logger.info("Item created",
item_id=..., item_type=...)`. Context goes in key-value fields, never in the
message. Application code never creates environment-specific loggers; each
entrypoint calls `configure_logging(service=..., platform=PLATFORM,
environment=ENVIRONMENT, level=LOG_LEVEL)` once. Two independent settings:

`PLATFORM` (where it runs) alone picks the implementation:

- `local`: standard `logging`, one readable line per record
  (`time LEVEL logger: message key=value ...`).
- `aws`: AWS Lambda Powertools `Logger` (optional `stash-shared[aws]`
  extra; falls back to standard logging with a warning if missing).
- anything else (e.g. `digitalocean`): standard `logging`, one JSON object
  per line.

`ENVIRONMENT` (the deployment stage: `local`, `dev`, `stage`, `prod`...)
only labels records, so e.g. dev, stage and prod on the same platform log
identically and are told apart by that field.

Every record has `timestamp`, `level`, `message`, `service` (the compose
service name), `platform` and `environment` (the JSON and Powertools
formats; the local format leaves out the last three), plus whichever
shared fields apply:
`request_id`, `job_id` (the queue message id), `queue`, `item_id`,
`user_id`, `item_type`, `item_status`, `storage_key`, `attempt`,
`max_attempts`, `duration_ms`, `error_type`. Exceptions keep their stack
trace.

Context is attached once per unit of work, not passed around: the API's
request middleware opens a context with a fresh `request_id` (the auth
dependency adds `user_id`, item operations add `item_id`) and logs one line
per request with route, status and duration; the `Worker` opens one per
delivery with `queue`, `job_id`, `attempt`, `item_id`, `user_id` and
`item_type`. Everything logged inside, third-party libraries included,
carries those fields. So an item's history can be followed by `item_id`
from the API request through each stage: `Job started`, `Job attempt
failed; retrying` (WARNING, with `error_type`, `retry_mode`, `delay_seconds`
on Valkey only, and the stack trace), then `Job completed`, or `Job moved to
dead-letter queue` (ERROR, with `reason`, `permanent`, `retry_mode`, and
`item_status=failed` when the item was failed; with `retry_mode=
visibility_timeout` it's SQS redrive that moves the message). External calls (OpenAI) are logged as `External call
succeeded/failed` with `operation`, `model` and `duration_ms`; storage
failures with `storage_key`.

Never logged: passwords, tokens, emails, note text, captions, filenames,
search queries (only their length), or document/image content — nor the
prompts sent to OpenAI or its answers (only `input_chars`/`input_bytes`,
`output_chars`, `model`, `response_status`). One temporary exception, off
by default: search's diagnostics (`Semantic search candidate`) can log each
candidate's best-matching chunk text while search is being tuned, with
`SEARCH_LOG_CHUNK_TEXT=true`, which the API refuses to start with unless
`ENVIRONMENT=local` (see "Search").

Exceptions are logged with their message and stack trace, so what goes into
an exception message is logged too. Hence: SQLAlchemy engines are created
with `hide_parameters=True` (a failed statement's error names the statement,
never its bound values); and an exception a parser or image decoder raised
on an upload, whose message may quote the file, is chained through
`stash_worker_core.errors.content_safe_cause`, which keeps its type and
stack trace but not its message (the dead-letter reason is e.g. `Could not
extract text: ValueError`).

As a backstop, every record is scrubbed as it's written, whatever logged
it — application code, a third-party library, an exception's stack trace —
in all three formats (`stash_shared.redaction`): values of fields with a
sensitive name (`authorization`, `cookie`, `password`, `*secret*`,
`api_key`, `access_token`/`refresh_token`/`*_token`...) are replaced
whole; in text, `Bearer`/`Basic` credentials, `name=value` pairs with such a
name, OpenAI keys, passwords in connection URLs, SQLAlchemy's
`[parameters: ...]`, `PASSWORD '...'` literals and every URL's query string
(a pre-signed URL's signature and credentials) are replaced by
`[REDACTED]`. It can't recognise user content in free text, so it doesn't
replace the rules above. Libraries that log requests (`openai`, `httpx`,
`botocore`...) are kept at WARNING whatever `LOG_LEVEL` is.

Inside a trace span (see "Tracing"), every record — third-party ones
included — also carries that span's `trace_id` and `span_id`, added by
`stash_shared.log` itself; application code never fetches them.

## Tracing

Distributed tracing uses OpenTelemetry, set up by `stash_shared.tracing`.
Each process calls `configure_tracing(service=..., environment=...,
enabled=TRACING_ENABLED, otlp_endpoint=TRACING_OTLP_ENDPOINT)` once, next
to `configure_logging` (the API in `app.main`, the workers via
`stash_worker_core.runtime.configure_observability`). Spans are exported
over OTLP/HTTP to whatever collector `TRACING_OTLP_ENDPOINT` names —
Jaeger locally. With tracing off nothing is set up and every span is a
no-op, so application code never checks whether it's enabled. A service's
name in traces is the same as in its logs (its compose service name;
`SERVICE_NAME` overrides it for both).

What's traced:
- Automatically: incoming HTTP requests (FastAPI; not `/health`), every
  SQL statement (SQLAlchemy), every S3 call (botocore).
- `stash_shared.log.logged_call` — every external API call (OpenAI) —
  opens a client span with its log fields as attributes.
- Business operations: the API's item create/update/delete/search and
  upload start/finalize, the
  thumbnail stage's image processing, the document stage's text
  extraction.
- The queue: `publish <queue>` (producer) for every published job and
  dead letter, `process <queue>` (consumer) for every delivery.

The trace follows an item through the queues. `JobQueue.publish` injects
the current W3C trace context into the message as metadata — on Valkey, a
`trace_context` stream field next to `payload`; the payload itself is
unchanged — and the `Worker` opens each delivery's span as a child of the
publish span that sent it. So an upload and every stage it triggers are
one trace:

    POST /uploads/{upload_id}/finalize (api)
      └ publish thumbnail_jobs
          └ process thumbnail_jobs (thumbnailer)
              └ publish content_analysis_jobs
                  └ process content_analysis_jobs (image_analyzer)
                      └ publish embedding_jobs
                          └ process embedding_jobs (embedding_worker)

Each delivery span has the log fields (`queue`, `job_id`, `attempt`,
`item_id`, ...) as attributes and an `outcome`: `completed`, `retry` (with
`retry_mode`, and `retry_delay_seconds` on Valkey), `dead_lettered` (with `dead_letter_reason`,
`permanent`) or `skipped` (with `skip_reason`). A retried job's attempts
are sibling spans under the same publish span — a redelivery is the same
message, with the same trace context — and failed attempts are marked as
errors with their exception recorded. Messages published without trace
context (e.g. before tracing existed) start a new trace. Tracing never
changes processing: retries, dead-lettering and acking are the same with
it on or off.

The API also sets the request's `request_id` on its HTTP span, so a
trace can be found from a log line and the other way round.

The same content rules as for logs apply to span attributes: no user
content, only ids, sizes, types and outcomes. SQL spans carry the
statement text with bind-parameter placeholders, never the values. OpenAI
call spans (`logged_call`) carry `purpose`, `model`, input/output sizes and
`response_status`, never the prompt or the answer. Spans are scrubbed on
export the same way log records are written (`RedactingSpanExporter`, around
the OTLP exporter): attributes, recorded exceptions (message and stack
trace) and status descriptions; and the HTTP server span's URL attributes
(`http.url`, `http.target`, `url.query`) lose their query string, which may
carry what the user typed (`GET /tags?query=...`), as the request log line
does. Request and response headers are never captured.

## Metrics

Operational metrics go through one abstraction, `stash_shared.metrics`
(`metrics.count`, `metrics.record_duration`, `metrics.gauge`,
`metrics.external_call`). Each process calls `configure_metrics(service=...,
platform=PLATFORM, environment=ENVIRONMENT, namespace=METRICS_NAMESPACE)`
once, next to logging and tracing, and `PLATFORM` alone picks the
implementation:

- `aws`: CloudWatch, as Embedded Metric Format (EMF) JSON lines on stdout,
  serialized by AWS Lambda Powertools Metrics (the `stash-shared[aws]`
  extra, as for logging). CloudWatch Logs extracts the metrics, so the
  service needs no CloudWatch API calls, only its stdout shipped to
  CloudWatch Logs (e.g. the ECS `awslogs` driver). Data points are buffered
  and written every 10 seconds, and at exit.
- anything else (`local`, `digitalocean`...): a no-op. There is no local
  metrics backend.

Application code never checks which one is active, and recording never
raises or changes behaviour.

Every metric carries `service` and `environment` dimensions and is published
with that one dimension set only. Each distinct metric name + dimension
values combination is a billed CloudWatch custom metric, so there are no
extra aggregated copies (each worker stage has one queue, and an
operation-wide duration would mix dependencies anyway), and dimensions are
low-cardinality only: never user, item, request or trace ids, storage keys
or API routes. Durations are published as individual values, so CloudWatch
computes p50/p95/p99 itself, and a duration's SampleCount is the number of
things timed, so there is no separate counter for it. Counts are summed per
flush: read them with the Sum statistic.

Namespace `Stash` (`METRICS_NAMESPACE`):

| Metric | Unit | Dimensions | Recorded by |
|---|---|---|---|
| `JobDuration` | Milliseconds | `queue` | `Worker` (all stages), every handler run, whatever its outcome |
| `JobFailures` | Count | `queue` | `Worker`, a handler run that raised |
| `JobRetries` | Count | `queue` | `Worker`, a failure sent back to the queue |
| `JobsDeadLettered` | Count | `queue` | `Worker`, for any reason |
| `QueueBacklog` | Count | `queue` | `Worker.run_forever`, sampled every 30 s (Valkey only; SQS publishes its own) |
| `QueueOldestMessageAge` | Seconds | `queue` | same |
| `ExternalCallErrors` | Count | `operation` | `logged_call` (OpenAI), the storage wrappers |
| `ExternalCallDuration` | Milliseconds | `operation` | same |

`operation` is `openai.responses`, `openai.embeddings`,
`storage.upload`, `storage.download`, `storage.inspect` (the API reading
back a finished upload's size and first bytes) or `storage.delete`. Any exception
counts as an external-call error, including a storage key that doesn't
exist or an input OpenAI rejects.

Reading them:
- API: from API Gateway's own metrics (`AWS/ApiGateway`, by `ApiId`):
  `Count`, `4xx`, `5xx`, `Latency` (p50/p95/p99) and `IntegrationLatency`.
  Per-route numbers come from the request log lines (`route`,
  `status_code`, `duration_ms`) with CloudWatch Logs Insights, e.g.
  `stats count(*), pct(duration_ms, 95) by route, method`.
- Workers: attempts = SampleCount(`JobDuration`); duration =
  p50/p95/p99 of `JobDuration`; failure rate = Sum(`JobFailures`) /
  SampleCount(`JobDuration`); retries and dead letters as Sums. Completed
  jobs are the SQS queue's `NumberOfMessagesDeleted` (skipped ones
  included).
- Queues: on SQS, the queue's own `ApproximateNumberOfMessagesVisible`
  and `ApproximateAgeOfOldestMessage`. `QueueBacklog` and
  `QueueOldestMessageAge` only exist for a backend that reports
  `JobQueue.stats()` (Valkey): messages not yet acked (waiting, in flight
  or waiting for a retry, from the consumer group's `lag` + `pending`) and
  how long ago the oldest of them was published, retries included. Every
  replica of a stage samples the same queue, so read both with Maximum.
- External dependencies: calls = SampleCount(`ExternalCallDuration`),
  p50/p95/p99 of it, and error rate = Sum(`ExternalCallErrors`) / calls,
  by `operation`.

Deliberately not recorded here, because AWS publishes them natively:
API Gateway request counts, errors and latency, compute CPU/memory, Lambda
invocations/errors/throttles/concurrency/duration, load balancer request
counts, RDS connections and resources, ElastiCache and SQS metrics.
Application metrics complement these. Nor are product counts (items
created...), which say nothing about health: those are product analytics
(below).

## Product analytics

What users do, for product questions (activation, retention, which features
are used), in PostHog: separate from logs, traces and metrics, which are
about the system's health. Setup and suggested insights:
[deployment.md](deployment.md#product-analytics-posthog).

Off unless configured, on each side separately: the API with
`ANALYTICS_ENABLED=true` plus `POSTHOG_PROJECT_API_KEY` and `POSTHOG_HOST`
(`app.config`), each client build with `STASH_ANALYTICS_ENABLED=true` plus
`STASH_POSTHOG_PROJECT_API_KEY` and `STASH_POSTHOG_HOST` (compiled in).
Settings default to off, so tests and unconfigured environments send
nothing; the API's tests replace it with a recorder.

### Identity

Each user has an `analytics_id` (`users.analytics_id`): random, generated
with the account, unrelated to its id or email, and used only as PostHog's
`distinct_id`. `GET /users/me` returns it, so the clients identify with the
same id the API uses: one person in PostHog for every device. No person
properties are set; nothing else about the account (email, id) is sent.
Clients send nothing until the account has loaded: events from before
(opening the app) wait for the id, and signing out drops what's still
waiting and forgets the identity (the next account's events never carry the
previous one's id). Events are kept after an account is deleted; they can't
be linked back to it once the row (and with it the mapping) is gone.

### What's sent

Only the properties each event allows, and only closed sets of values:
types, modes, flags, sizes and counts. Never content, descriptions,
filenames, URLs, search queries, tag names or ids, passwords, tokens,
emails or IP addresses. On the API, `app.analytics.EVENT_PROPERTIES` and
`ALLOWED_VALUES` drop anything else (at capture, and again in the SDK's
`before_send`, which also strips what the SDK adds about the server); in
the clients, events are typed (`analytics` crate), so nothing else can be
built. GeoIP is disabled on every event (`$geoip_disable`). The API's
requests carry the Lambda's IP, never the user's; the clients send to
PostHog directly, so the PostHog project must have *Discard client IP
data* on (a required setup step). There's no autocapture, pageview, session
replay or exception capture: the clients don't use PostHog's JavaScript
SDK, only its capture API, and the API's SDK has exception capture and
feature flags off.

### Events

Each action is tracked in one place only. The API's events are captured by
the service that commits the action, after its commit, so they mean it
happened: failed, rejected, rolled back and no-op requests send nothing.

| Event | Where | Properties |
|---|---|---|
| `session_started` | client | `platform` (web, desktop, mobile), `mode` (normal, blind) |
| `mode_changed` | client | `mode` |
| `item_opened` | client | `item_type`, `from_search`, `mode` |
| `sort_changed` | client | `sort_method` (date, random), `sort_direction` (asc, desc; date only) |
| `filters_changed` | client | `file_type_filter`, `tag_filter`, `favourites_filter` (bools), `active_filter_count`, `selected_tag_count` |
| `account_settings_opened` | client | — |
| `privacy_opened` | client | — |
| `account_registered` | API | — |
| `item_saved` | API | `item_type`, `size_bytes` (images, files) |
| `search_completed` | API | `result_count` |
| `item_description_edited` | API | `item_type` |
| `item_tags_changed` | API | `action` (add, remove), `tag_visibility` (regular, hidden), `affected_item_count` |
| `tag_visibility_changed` | API | `visibility` (hidden, visible), `affected_tag_count` |
| `item_favourite_changed` | API | `action` (added, removed), `item_type` |
| `items_deleted` | API | `deleted_count`, `text_count`, `link_count`, `image_count`, `file_count` |
| `password_changed` | API | — |
| `account_deleted` | API | — |
| `language_changed` | API | `language` (en, uk) |

Where each event comes from, and how it's counted:

- Language and hidden tags are kept with the account, so their changes are
  the API's events; the Normal/Blind mode is per device, so its change is
  the client's.
- `session_started`: opening the app, and the first activity after 30
  minutes without any (`analytics::SessionClock`). Activity is the user's
  input only (pointer, keys, wheel, and every tracked action): rerenders,
  timers, background refreshes and a tab left in the background never
  start or extend a session. Signing out ends the session; another account
  identified starts its own.
- `item_saved`: once per item created, on `POST /items/text` and on the
  finalize that creates an upload's item; a retried finalize returning the
  existing item, a rejected upload, or a started upload never finalized
  sends nothing. `size_bytes` is the stored size.
- `item_tags_changed`: one event per tag added to or removed from one item
  (`affected_item_count` 1), whatever the path: `POST /items/{id}/tags`,
  `DELETE /items/{id}/tags/{tag_id}`, and each tag given when an item is
  saved (counted exactly as if added afterwards, so tagging on save and
  tagging later add up the same; the save itself is the `item_saved`).
  Adding a tag already on the item, or removing one that isn't, sends
  nothing. `tag_visibility` is the tag's at the time (read before a
  removal, which may delete the tag). A tag created by its first use, or
  deleted with its last item, isn't an event of its own; neither is a
  tag's disappearance when the last item carrying it is deleted. The web
  client applies a bulk change to selected items one item at a time, so
  that's one event per item: sum `affected_item_count` for items, count
  events for tag changes.
- `tag_visibility_changed`: one per `POST /tags/visibility` that changed
  something, `affected_tag_count` the tags whose state actually changed.
  Uploading the hidden tags a device kept on its own before they were kept
  with the account (`imported_from_device`) isn't counted.
- `item_favourite_changed`: only when the state changes (marking a favorite
  again is a no-op). There's no favourite on save.
- `items_deleted`: one per committed `DELETE /items/{id}` or
  `POST /items/delete`, counting only items actually deleted (ids that
  aren't the user's, or are gone, aren't); nothing if none were. The web
  client deletes a selection item by item, so that's one event per item.
  Account deletion is `account_deleted` only.
- `item_description_edited`: a `PATCH /items/{id}` that changed the user's
  text (a note's or link's text, an image's or file's caption, including
  removing it); a rename, a type change or an unchanged text isn't one.
- `search_completed`: once per search the user makes, after it succeeded
  (a 503 or 429 isn't). The client sends `rerun: true` on `POST /search`
  for a query it already got results for (results refreshed after an edit,
  a favorite or a filter change, a restart), and those aren't counted; the
  query is forgotten when the search box is emptied, so searching it again
  later counts. Search has no pagination, and listing (`GET /items`) is
  never a search. Search-as-you-type sends a query after each pause in
  typing, so "cat", a pause, then "cats" are two searches. A request the
  client abandons while it's in flight (the user typed on, or changed a
  filter) still counts if the server completed it, and the client, not
  having seen its results, sends the next one as new.
- `item_opened`: an item opened in the viewer from a card, each item
  stepped to in the viewer, each item "Surprise me" shows, and a file or
  link opened from its card. Switching between viewing and editing isn't.
  `from_search`: opened from search results.
- `filters_changed`: when the type, tag or favourites filter changes (by
  the user: tags dropped from the filter because they no longer exist
  aren't); the date and collection filters aren't tracked. Changing the
  order (`sort_changed`) is separate; reshuffling a random order isn't a
  change.
- `privacy_opened`: opening the Privacy section of the settings.
- Collections are hidden in the clients and aren't tracked.

### Delivery

Analytics never gets in the way of what it records, and loses events rather
than wait:

- API: `PostHogAnalytics.capture` puts the event on the SDK's bounded queue
  (`ANALYTICS_MAX_QUEUE_SIZE`, 1,000; full drops) and never raises; the
  SDK's consumer thread sends it, each request bounded by
  `ANALYTICS_TIMEOUT_SECONDS` (3 s), one retry. A Lambda environment is
  frozen as soon as the handler returns, and nothing guarantees the
  consumer thread runs after that, so the handler flushes at the end of
  every invocation, after metrics and traces, bounded by
  `ANALYTICS_FLUSH_TIMEOUT_SECONDS` (2 s); what isn't sent by then may go
  with a later invocation, or be lost. Only requests that captured
  something wait for it.
- Clients: events go straight to PostHog's batch endpoint, never through the
  API, from a bounded queue (100 events; the oldest dropped), one request
  at a time (at most 50 events), abandoned after 5 s, never retried. The
  sending runs detached from the UI, so closing a view doesn't cancel it.

## Environments

### Local

The complete application runs locally using Docker Compose.

Expected services:
- API
- Workers (thumbnailer, image analyzer, document analyzer, embedding worker)
- PostgreSQL
- MinIO
- Queue
- Jaeger (traces from every backend service; UI at http://localhost:16686,
  in-memory, so traces are lost on restart)

Configuration is provided through local environment variables / `.env`.

### Production

Production runs on AWS.

Infrastructure (Terraform, `infra/terraform/live/`):
- One Lambda per service: the API (`app.aws_lambda.handler`, the FastAPI
  app through Mangum, behind API Gateway) and one per worker
  (`<worker>.aws_lambda.handler`, SQS-triggered), each with its own
  least-privilege execution role, in a private subnet (IPv4 to RDS, IPv6
  out through an egress-only internet gateway, no NAT). Plus the migration
  function (see PostgreSQL), invoked only by the deployment. An
  EventBridge rule also invokes the API function every 15 minutes to drain
  pending object deletions, and another hourly to look for orphaned
  objects (see "Deleting stored objects")
- RDS PostgreSQL (with pgvector), Single-AZ
- Amazon S3
- SQS queues with DLQs, each with a resource policy admitting only its
  producers and its worker
- The web frontend: a private S3 bucket served only through CloudFront
  (Origin Access Control, `*.cloudfront.net`, SPA fallback to
  `index.html`); its origin is always in the API's and the object bucket's
  CORS origins

The Lambdas run the same code as the local services, packaged as one zip
per function (`scripts/build_lambda_packages.py`), with environment-specific
configuration.

CI/CD is GitHub Actions ([deployment.md](deployment.md)). Pull requests and
`main` are validated (tests, Terraform fmt/validate, plus a read-only plan
on pull requests). Production is deployed only when someone starts the
deployment workflow by hand: tests → Lambda packages → the migration
Lambda alone (a targeted Terraform apply) → migrations → Terraform apply of
everything else → frontend build and upload → smoke tests. Schema first:
new code never meets a schema that lacks what it uses. AWS access is through
GitHub OIDC roles scoped to Stash's resources
(`infra/terraform/github_oidc`), never stored keys.

Secrets are plain settings locally (`DATABASE_URL`, `OPENAI_API_KEY`). On
AWS the functions get Secrets Manager ARNs instead (`DATABASE_SECRET_ARN`,
`OPENAI_API_KEY_SECRET_ARN`), each only the ones it uses: its own database
login (see "Database roles"), the OpenAI key only for the API and the
three workers that call OpenAI, the Turnstile key only for the API. The
settings classes resolve them into those
same fields when they're built (`stash_shared.secrets`): once per execution
environment, at cold start, never per request or message. Application code
only ever sees the settings. The exception is the API's OpenAI key, which
only search uses: it's fetched when search first needs it
(`app.config.get_openai_api_key`), then kept. So the API starts, and
everything but search works, while that secret is unset or unreadable.
Search then answers 503, and tries again on the next request.

## High-level Flow

### Save Text

Client → API → PostgreSQL → Queue → Worker → PostgreSQL

### Save File

Client → API (start upload) → PostgreSQL (`pending_uploads`)
Client → Object Storage (`users/{user_id}/files/{item_id}[.{ext}]`, pre-signed PUT)
Client → API (finalize) → PostgreSQL (`item_files`)
                        → document_analysis_jobs → Document Analyzer → OpenAI
                                                                     → PostgreSQL

See "Uploads" for the upload itself.

Only `analyzable` formats are enqueued (item created `pending`); anything
else is created `completed` with no processing. The document analyzer
(`document_analyzer` service) extracts the file's plain text with a
per-format parser (`document_analyzer.parsers`), caps what it sends
to OpenAI at `DOCUMENT_ANALYSIS_MAX_CHARS` characters — longer documents are
represented by their beginning plus evenly spaced samples — and stores a
short description of what the document is and is about (not a summary) in
`item_descriptions`, after any caption. It uses the same `Worker` as the
image stages, so retries, backoff, the 5-attempt limit, dead-lettering (to
`stash:document_analysis_jobs:dead-letter`) and ack-after-durable-outcome
all behave identically. Unparsable, unsupported or text-less files (e.g.
scanned PDFs: there is no OCR) fail immediately without retries.

Files are uploaded up to 500 MB, but a description needs a few thousand
characters, so how much of a file the analyzer reads, inflates and parses
is limited on its own (`document_analyzer.parsers`, `DOCUMENT_MAX_*`
settings), far below the upload limit:

| Format | Read from storage | Other limits |
|---|---|---|
| Plain text (TXT, MD, CSV, JSON, YAML...) | up to 8 MB whole; larger, only the ranges the excerpts come from (a few hundred KB) | — |
| HTML, XML, FB2, RTF | the first 8 MB (`DOCUMENT_MAX_PREFIX_BYTES`) | XML streamed, entities refused |
| DOCX/XLSX/PPTX, ODT/ODS/ODP, EPUB, FB2.ZIP | ranges: the ZIP directory, then only the members parsed (never media), at most 64 MB (`DOCUMENT_MAX_BYTES_READ`) | 10,000 entries (declared, checked before the directory is read, and listed); 64 MB decompressed, all members together, counted as inflated; per member at most 64 MB and 200:1 compression (past 1 MB); encrypted members and members outside the archive (`..`, absolute: zip-slip) refused; never extracted to disk; XML streamed, 2,000,000 elements |
| PDF | the whole file, to local disk (`/tmp`), never into memory, up to 512 MB (`DOCUMENT_MAX_DOWNLOAD_BYTES`); pypdf reads at most 64 MB of it | 10,000 pages in all; 8 MB read and decompressed to open it (cross-references, page tree); 50 pages read (half from the start, half spread over the rest), each only if its content (with its form XObjects) is at most 2 MB, else skipped; 64 MB decompressed in all, 16 MB per stream |

For every format: at most 240,000 characters extracted (10x what's sent
to OpenAI), and 60 s of parsing (checked between pages, members, chunks,
every thousand XML elements, every thousand PDF operators, and while
pypdf reads the file). One uninterruptible library call can overrun it by
a few seconds; the function's timeout is the backstop. PDFs
are the one format read whole: objects can be anywhere, and pypdf checks
the header of every object in the cross-reference table as it opens a file
(to recover damaged ones), which would fetch most of the file range by
range anyway. A limit reached after some text was extracted stops there
and the text so far is described (as excerpts); reached before any, the
item fails, permanently. The upload itself is never rejected for it. A
storage error while parsing is retried like any other. Failures are logged
and traced with an error category (`stash_worker_core.errors.ErrorCategory`:
`TRANSIENT`, retried; `MALFORMED_INPUT`, `PROCESSING_LIMIT_EXCEEDED` and
`PERMANENT`, never retried); users only ever see the item as failed.

The listing's pre-signed `download_url` serves the file under its original
filename: inline for PDF, plain text and JSON, as a download for everything
else. Clients open a file from a URL fetched when it's clicked, not the
listed one, which may have expired (see "Expired listing URLs" under
"Object Storage").

### Save Image

Client → API (start upload) → PostgreSQL (`pending_uploads`)
Client → Object Storage (`users/{user_id}/images/{item_id}.{ext}`, pre-signed PUT)
Client → API (finalize) → PostgreSQL
                        → thumbnail_jobs → Thumbnail Worker → Object Storage (thumbnail)
                                                           → PostgreSQL
                                                           → content_analysis_jobs → Content Analyzer → OpenAI
                                                                                                      → PostgreSQL

### Search

Client → API → PostgreSQL filename match (query as typed)
             → OpenAI Responses (query → English) → OpenAI Embeddings (English query)
             → PostgreSQL pg_trgm user-text match (query as typed)
             → PostgreSQL full-text description match (English query)
             → PostgreSQL + pgvector best-chunk similarity search (English query embedding)
             → Results (one list, in that order of tiers)

Search is hybrid: four matches, ranked in tiers in this order. An item
found by several is listed once, in the first tier that found it. Text
the user wrote (filenames, notes, captions) can be in any language, so
it's matched against the query as typed; the generated descriptions are
in English, so they're matched against the English rewrite (below). The
literal matches exist because the embedding alone can miss short queries,
and loosening the semantic threshold would let unrelated items back in.
That's also why items are embedded as several short chunks rather than
one text: a whole image description embedded at once scored "city" ~0.8
cosine distance from "a futuristic cyberpunk city", past the threshold,
because the concept was diluted by everything else the image shows.

1. Filename match (`ItemRepository.search_by_filename`): files and images
   whose filename (`item_files.filename` / `item_images.filename`) contains
   every word of the query as typed (runs of letters and digits,
   case-insensitive), so "resume 2" and "Resume-2.PDF" both find
   `resume-2.pdf`. An exact filename match comes first, then newest first.
   It's a plain `LIKE` scan of the user's items, with no index, which is
   fine at one user's scale; `pg_trgm` is the option if that changes. The
   query isn't normalized for this part, since translating it would break
   names. Renames apply immediately. Images uploaded before their name was
   kept have no name to match.
2. User-text match (`ItemRepository.search_by_user_text`): items whose own
   text (`item_text_contents`: a note's or link's text, an image's or
   file's caption) contains something close to the query as typed, by
   `pg_trgm`'s `word_similarity` (the share of the query's trigrams found
   in the text's closest stretch) of at least
   `SEARCH_MIN_TEXT_SIMILARITY` (default 0.6), most similar first.
   Trigrams are language-neutral and forgiving: "город" finds "городу",
   "tody" finds "today". A plain scan of the user's items, no index.
3. Description match (`ItemRepository.search_by_description`): items whose
   `item_descriptions` text (generated description — for an image, all its
   search chunks — plus caption) contains every word of the English query,
   so "city" finds an image with a "cyberpunk city" chunk however far that
   chunk is by meaning, by Postgres full-text search
   (`websearch_to_tsquery('english')` against the generated, GIN-indexed
   `search_vector` column; English stemming, so "cities" finds "city"),
   best `ts_rank` first. A query of only stopwords ("the") matches nothing
   here.
4. Semantic match, below: items with a chunk closest in meaning.

All four use the same filters and together return at most `limit` items.
If the query can't be embedded, the whole search is unavailable (503),
even when some other part matches.

What's embedded comes from each item's `item_descriptions` text — the
single searchable text per item (a note's/link's text, a caption, a
generated image/document description, or caption + description) — split
into search chunks (`stash_shared.descriptions.search_chunks`): the
user's own text (note, link, caption) is one chunk however many lines it
has, and each line of the generated description is a chunk of its own. So
an image is its caption plus each of the analyzer's chunks; a document's
description is normally a single chunk. Embedding is its own asynchronous
stage, separate from content analysis:

    text/link item created, or upload with a caption → API ─┐
    image/document description saved → content analyzer ───┴→ embedding_jobs
        → embedding_worker → OpenAI Embeddings (all chunks, one request) → item_search_chunks

Events carry only the item id; the embedding worker reads the current
description when it runs. `item_search_chunks` holds one row per chunk:
its position, text and `vector(1536)` (`text-embedding-3-small` by default,
`EMBEDDING_MODEL`; the API and worker must use the same model), deleted
with the item (`ON DELETE CASCADE`). When the text changes, all of an
item's chunks are replaced in one transaction, never added to: the worker
row-locks the description, checks it still splits into exactly the chunks
it embedded (if not, it writes nothing: a newer job is on its way), then
deletes the old rows and inserts the new ones. Text whose chunks are
already stored isn't re-embedded. The embedding worker uses the same
`Worker` as the other stages (retries, 5 attempts, dead-letter queue) but
never changes an item's status. Its events go through the outbox, in the
same transaction as the description they're for. An embedding job
dead-lettered after its retries (e.g. an OpenAI outage longer than them)
leaves the item without up-to-date chunks until the job is replayed from
the dead-letter queue or the text changes again. Items saved before search
chunks existed have none until their text is embedded again (re-analysis
or an edit); until then they're found by the literal matches only.

Before a query is embedded, the API has a small, fast model
(`SEARCH_QUERY_NORMALIZATION_MODEL`, default `gpt-5-nano`, minimal
reasoning) rewrite it into concise English with the same meaning
(`app.query_normalization`): searchable text is mostly English, since the
generated image and document descriptions are always written in English
(the describer prompts ask for it, whatever the content's language), and a
Ukrainian query otherwise lands further
from it than the same query in English. English queries are kept as they
are. The rewrite is embedded and used for the description full-text match;
the filename and user-text matches use the query as typed.
The rewrite is best-effort: if the call fails, times out
(`SEARCH_QUERY_NORMALIZATION_TIMEOUT_SECONDS`, default 5, no retries) or
returns something unusable (empty, or far longer than the query), the
original query is embedded instead and the search still succeeds. Neither
the query nor its rewrite is logged, only lengths and whether it was
rewritten.

`POST /search` embeds the query once, with the same model, and compares it
with every chunk of the user's (filtered) items by cosine distance (`<=>`).
An item's distance is that of its best-matching chunk (`DISTINCT ON`
item, nearest chunk first: a per-item `MIN` that also keeps which chunk
it was), so each item is listed once, nearest first
(`ItemRepository.search_by_chunks`). Items whose best chunk is further than
`SEARCH_MAX_COSINE_DISTANCE` (default 0.6: short chunks score real matches much closer than whole descriptions did, see `app.config`) are left out, so unrelated items
aren't returned. Items without chunks yet aren't semantically searchable.
This computes the distance to all of a user's chunks exactly, which is
fine at one user's scale. `item_search_chunks.embedding` already has an
HNSW index (`vector_cosine_ops`) for when that changes: the query would
then take its candidates from an inner `ORDER BY embedding <=> query LIMIT
n` over the chunks (with `hnsw.iterative_scan`, so the per-user filter
doesn't truncate them) and keep each item's best of those.

Temporary diagnostics, while search is tuned: each search logs one
`Semantic search candidate` line per item the semantic match considered
(`item_id`, `cosine_distance`, `similarity`, `passed_threshold`, and
`best_chunk` — its text, the one content the logs otherwise never carry —
only with `SEARCH_LOG_CHUNK_TEXT=true`, allowed only with `ENVIRONMENT=local`),
and one `Search result` line per returned item with `match_sources`: every
tier that found it (`filename`, `user_text`, `description`, `semantic`).

### Tags, collections, favorites and filtering

Users label items with their own tags (`tags`: one row per user and name,
unique per user case-insensitively; `item_tags`: the many-to-many link).
Tags are private to their owner. Assigning by name reuses the user's
existing tag of that name or creates it. Listing (`GET /items`) and
semantic search (`POST /search`) share the same server-side filters: an
item type, item kinds (below; an item may be of any of them), any number of tags (an item must carry all of them),
any number of collections (an item must be in any of them; see below),
favorites only (`items.is_favorite`, toggled per item), and a saved-date
range (`created_from` inclusive, `created_before` exclusive, both with a
time zone offset). The server has no notion of the user's time zone:
clients pick a year, month or day on their own calendar and send its
bounds, so "2025" means 2025 where the user is.

On top of those, `exclude_tag_id` (repeatable, up to 100; `excluded_tag_ids`
in search) leaves out every item carrying any of the given tags, in a
`NOT EXISTS` on `item_tags`. It backs the clients' Blind mode (hiding items
with the tags the user marked hidden), so `GET /items/counts` and
`GET /items/random` ("Surprise me") take it too, and an item hidden from the
list isn't counted or surprised with either. The hidden tags are kept with
the account (`tags.is_hidden`; `GET /tags/hidden`, `POST /tags/visibility`,
at most 100), so every device hides the same ones, and go with the tag;
the mode is each device's own. The server still doesn't apply them by
itself: the client sends the ids while Blind mode is on, and an id that
isn't one of the user's tags (e.g. a tag since deleted) excludes nothing.

Listing is newest first by default; `sort=oldest` reverses it (same
keyset pagination on `created_at, id`, the cursor recording its order),
and `sort=random` returns `limit` matching items via `ORDER BY random()`.
The shuffle is applied after the `user_id` and filter conditions, so it
only sorts the user's own matching items (a scan of those, fine at a
personal stash's size). A new shuffle every request can't be paged, so a
random listing has no next page. Search always ranks by relevance.

`GET /items/years` tells the date picker which years have items, for the
same reason without year numbers: per calendar year it returns the first
and last `created_at` (one `GROUP BY` over the user's items). A client
converts both to its own time zone; every year between the two has items,
since a calendar year in one zone overlaps at most two in another.

Kinds group images and files for the clients' Media (`image`, `video`,
`audio`) and Files (`document`, `book`, `other`) filters. Images are
always `image`. A file's kind isn't stored: it follows from its stored,
validated `item_files.content_type`, and `app.items.files` is the one
place that maps formats (and so content types) to kinds. PDFs are
documents, since nothing reliable marks one as a book; `other` is
whatever isn't one of the rest (archives, generic files). The filter
matches the exact stored types of the kind, in an `EXISTS` on
`item_files`. Files uploaded before a format was recognized (e.g. video
before it was) stay generic, and so `other`.

`GET /items/counts` returns how many items the user has of each type and
each kind (every one present, zero if none) and how many are favorites,
for the filter controls. It's one `COUNT(*)` over `items` left-joined to
`item_files`, grouped by type and content type and scoped to the user;
the API maps each content type to its kind. There are no stored counters
to keep in sync. Deletes are hard deletes, so there's nothing to exclude.

A tag exists only while some item uses it. Removing a tag from an item, or
deleting an item, also deletes each affected tag that no item uses any more,
in the same transaction. Row locks keep this safe against concurrent
tagging (`app.tags.repos.TagRepository`):

- Cleanup locks the tags `FOR UPDATE` before checking for remaining links.
- Linking a tag (tagging an item, or saving a new item with tags) locks it
  `FOR KEY SHARE` in the same transaction as the link, and creates it if
  it's gone. New items' tags are linked in the item's own transaction for
  this reason.
- Deleting an item locks the item first, so no tag can be linked to it
  after its tags were read.

Each side therefore waits for the other to commit. A tag is never deleted
while a link to it exists or is being made.

`GET /tags/suggestions` offers existing tags when one is being added (6 by
default): the most recently used first, filling at most half the list,
then the most frequently used, each tag once; with `item_id`, tags already
on that item are left out. Usage is computed on each request from
`item_tags` — uses as `COUNT(*)`, recency as `MAX(item_tags.created_at)`
(when each link was made), grouped by tag — so there's no usage counter to
keep in sync. The user is resolved on `tags` (`user_id` leads its unique
index), and `ix_item_tags_tag_id_created_at` covers the per-tag count and
latest use; `item_tags` has no `user_id` of its own.

Collections group items (`collections`: one row per user and name, unique
per user case-insensitively; `item_collections`: the many-to-many link, so
an item can be in any number of them). Like tags they're private and
assigned by name, reusing the user's collection of that name or creating
it, on their own (`POST /items/{id}/collections`) or as an item is saved
(`collections` on `POST /items/text` and `POST /uploads`; the client sends
the same names with every file of a batch). Like a tag, a collection
exists only while some item is in it: taking the last item out, or
deleting it, deletes the collection in the same transaction, with the same
row locks as tags (`app.collections.repos.CollectionRepository`). Where a
transaction locks both, tags come first, so the two can't deadlock.

One thing differs from tags: filtering by several collections matches
items in *any* of them (a union, like opening several folders), where
several tags narrow. With both, an item must satisfy both
(`app.items.repos.ItemFilters`).

`GET /collections` lists them (containing a query, prefix matches first,
then by name), for the clients' collection picker and filter.

### Notes and links

A saved text's type (`text` or `link`) is decided once, when it's saved or
edited, and stored on the item; filtering, counts and clients read the
stored type and never infer it from the content again
(`app.items.services.resolve_text_item_type`):

- Only a URL (a single http(s) URL with a host) → `link`.
- No URLs → `text`.
- Text and URLs (or several URLs) → whichever the user chose. Clients
  detect this case with the same rules and offer a Text/Link choice. With
  no choice sent, a new item is `text` and an edited one keeps its type.

### Editing items

`PATCH /items/{id}` edits an item in place (its id never changes):

- Notes and links: the whole text, and the type. The type is resolved from
  the resulting text with the same rules as on creation (below), so a note
  can become a link and vice versa.
- Images and files: the caption. The description is rebuilt as the new
  caption plus the generated description (`stash_shared.descriptions`,
  also used by the content analyzers), under a row lock on the item so an
  analyzer finishing at the same moment can't overwrite the edit or be
  overwritten. With nothing searchable left, the description and search chunks
  are removed.
- Files: the displayed filename, which is also what downloads are named.
  The storage key and stored object never change.

When the searchable text changes, the item goes through `embedding_jobs`
again, as on creation.

### Current user

`GET /users/me` returns the signed-in user's `id` and `email`. Tokens are
opaque, so this is how clients learn who is signed in (the app uses the
email for the avatar's initials). Also their `analytics_id` (see "Product
analytics") and the UI `language` they chose, if any (`PUT
/users/me/language`, `en` or `uk`): kept with the account, so every device
they sign in on uses it.

### Changing the password

`POST /users/me/password` takes the current password and a new one. It
ends every session of the user (access tokens deleted, refresh tokens
revoked), so a leaked token stops working, and returns a fresh token pair
that keeps the caller signed in. A wrong current password is a 422 on the
`current_password` field, not a 401, which clients take to mean the
session itself is gone.

### Deleting the account

`POST /users/me/delete` takes the user's password (wrong ones count
against the same per-user limit as password changes, and get the same
422 on the field) and permanently deletes the account. POST rather than
DELETE because it has a body.

In the database it's one transaction: deleting the `users` row deletes
everything the user owns by `ON DELETE CASCADE`: items (and through their
own cascades, text, image and file rows, descriptions, search chunks, tag
and collection links), tags, collections, pending uploads, and access and
refresh tokens, so every session ends with it. Rate-limit counters (hashed
subjects that expire) and outbox events (ids only) are left; a queued job
for a deleted item is dropped by its worker, which finds the item gone.

Stored objects can't be deleted in that transaction, so, as for deleted
items (see "Deleting stored objects"), the transaction records what to
delete in `storage_deletions`: the user's two prefixes, `users/{user_id}/`
(originals and thumbnails) and `uploads/{user_id}/` (staged uploads), plus
the keys of any of their objects stored under the old unscoped layout. A
prefix deletion lists and deletes under it (at most 5,000 objects per
prefix per drain, so a drain stays bounded), and its row is removed only
once nothing is left.

Races: an upload being finalized holds its pending upload's row lock, which
the cascade waits for, so its item is either deleted with the account or
never created. A thumbnail the worker stores after its item is gone is
deleted by the worker itself (recording it finds no item;
`thumbnailer.handler`). An upload URL
issued before the deletion stays valid until it expires and can still put
an object under `uploads/{user_id}/`, which the staging lifecycle rule
expires within a day.

Copies outside the live database and bucket (RDS backups and snapshots,
what OpenAI received, logs) are covered in "Data retention and privacy".

### Deleting stored objects

Objects are deleted from storage only once the database no longer needs
them, and never inside a database transaction, which can't include them.
Two mechanisms, both in the API (`app.storage`):

Scheduled deletions (`app.storage.deletions`), a transactional outbox for
deletes. Deleting items (`DELETE /items/{id}`, `POST /items/delete`) or an
account records each object (or prefix) to delete as a `storage_deletions`
row, in the same transaction as the rows' deletion: the rows go and the
deletion is pending, or neither. After the commit the request drains its
own rows (only those: a request never takes on a backlog). A deletion that
fails stays, with its attempt count; the drain stops there (storage is
likely down) and a later drain retries it. So storage failing after the
commit, or the process dying, never orphans an object, and an item never
points at a missing one. On AWS an EventBridge rule invokes the API
function with `{"task": "drain_storage_deletions"}` every
`storage_deletion_drain_minutes` (15), which works through whatever is
pending. Rows that keep failing go to the back of the queue, so they can't
hold up the rest; from the 100th failed attempt each one is logged as an
error. Concurrent drains claim rows with `FOR UPDATE SKIP LOCKED`, like the
outbox, and deleting is idempotent, so a drain dying before it removed its
row only repeats a deletion.

Reconciliation (`app.storage.reconciliation`), for objects no deletion was
ever scheduled for, because whatever stored them never recorded them:

- a finalize that died between its `CopyObject` and its commit (see
  "Uploads"), or that rejected its copy but couldn't delete it;
- a thumbnail the worker stored after its item was deleted (or stored,
  then the item was deleted before the thumbnail was recorded), when the
  worker then failed to delete it itself (`thumbnailer.handler`);
- objects of items deleted before deletions were scheduled.

It scans `users/` in key order, one bounded run at a time (about 20 s,
pages of 1,000 keys), resuming where the last run stopped
(`storage_reconciliation`); after the last page the next run starts over.
It deletes an object only when all of these hold:

1. Its key is one the application writes there: a canonical original
   (`users/{user}/images|files/{item}/{object_id}{ext}`), a legacy one
   (`users/{user}/images|files/{item}{ext}`) or a thumbnail
   (`users/{user}/thumbnails/{item}.webp`), ids exactly as generated.
   Anything else is never deleted.
2. It was last written over a day ago (S3's `LastModified`; never less
   than an hour, enforced). A finalize holds its copy unreferenced only
   until its commit, a thumbnail worker its thumbnail until it records it:
   seconds, within a request's or a job's timeout.
3. No row references it: the key is looked up exactly in every column that
   holds one (`item_images.storage_key`/`thumbnail_key`,
   `item_files.storage_key`, each indexed), never inferred from the ids in
   it.
4. A thumbnail's item no longer exists. The worker stores a thumbnail
   before recording it, and a redelivered job stores it again at the same
   key, so an existing item may still come to reference it; a deleted item
   never comes back (ids aren't reused).

That's enough for originals because an original's key is random and new
for every copy, and the only code that makes a row reference one is the
finalize that made that copy, in the same request: an old, unreferenced
original can never become referenced. As a brake against deleting content
by mistake (say, the API pointed at the wrong database, where everything
looks unreferenced), one run deletes at most 100 objects and logs an error
when it reaches that; the rest wait for the next runs. Every deletion is
logged with its key. A storage or database failure ends the run without
moving the cursor, so the next run looks at the same objects again
(deciding again is safe). One run at a time: a run holds the cursor row
`FOR UPDATE`, and a concurrent one finds it locked (`SKIP LOCKED`) and
does nothing. `uploads/` isn't scanned: the staging lifecycle rule expires
it.

On AWS an EventBridge rule invokes the API function with `{"task":
"reconcile_storage"}` every `storage_reconciliation_minutes` (60). Locally
nothing schedules either task; run one in the API container with
`python -m app.storage.tasks drain_storage_deletions` (or
`reconcile_storage`).

### Rate limits and quotas

Endpoints that can be brute-forced, or that cost money (OpenAI, storage),
are limited per IP, per account and per user (`app.rate_limits`): that's
the abuse protection. A limit is one or more fixed windows, configured as a
setting like `"5/5m,10/30m"`; a request must fit in every window. Over any limit, the API answers 429 with
`Retry-After` (seconds until the window that blocked it ends) and one
generic message; which limit it was is only logged (`Rate limit
exceeded`, with `limit`).

| Setting | Counts | Per | Default |
|---|---|---|---|
| `LOGIN_LIMIT_PER_IP` | every login attempt | IP | 20/1m, 100/1h, 500/1d |
| `LOGIN_FAILURE_LIMIT_PER_ACCOUNT_IP` | failed logins | email + IP | 5/5m, 10/30m |
| `LOGIN_FAILURE_LIMIT_PER_ACCOUNT` | failed logins | email | 20/15m, 50/1h |
| `REGISTRATION_LIMIT_PER_IP` | registrations | IP | 10/1h, 30/1d |
| `REGISTRATION_LIMIT_PER_EMAIL` | registrations (409s too) | email | 10/1h |
| `TOKEN_REFRESH_LIMIT_PER_IP` | token refreshes | IP | 60/5m, 1000/1d |
| `PASSWORD_CHANGE_FAILURE_LIMIT_PER_USER` | wrong current passwords | user | 5/15m, 20/1d |
| `UPLOAD_LIMIT_PER_USER` | uploads started | user | 100/5m, 1000/1d |
| `UPLOAD_BYTES_QUOTA_PER_USER` | declared bytes of uploads started | user | 5 GiB/1d |
| `AI_ANALYSIS_QUOTA_PER_USER` | uploads that will be analyzed | user | 300/1h, 1000/1d |
| `SEARCH_LIMIT_PER_USER` | searches | user | 60/1m, 3000/1d |
| `ITEM_WRITE_LIMIT_PER_USER` | notes/links created, items edited | user | 120/5m, 3000/1d |

`RATE_LIMITS_ENABLED=false` turns them all off. Also enforced, as plain
settings: `MAX_IMAGE_UPLOAD_BYTES` (100 MB) and `MAX_FILE_UPLOAD_BYTES`
(500 MB), on the declared size and on what arrived (413). The bytes go
straight to S3 with the pre-signed PUT, so the API never holds them.
Every text field has a maximum length (422): notes and captions 100,000
characters, search queries 1,000, filenames 1,000 (stored cut to 255),
tag names 200 as sent (50 after trimming), emails 254, passwords 72
characters and 72 UTF-8 bytes (bcrypt's limit; 256 accepted at login),
Turnstile tokens 2,048.

Request bodies (JSON only) are limited per endpoint category
(`app.body_size`), 413 before they're parsed, by `Content-Length` or
counted as they stream. Each limit is sized from the category's longest
valid request in the worst encoding: a character takes at most 12 bytes
in a JSON string (an emoji as an escaped surrogate pair, `\ud83d\ude00`,
from clients that escape non-ASCII), 4 as raw UTF-8.

| Category | Setting | Limit | Longest valid body, worst case |
|---|---|---|---|
| content: `POST /items/text`, `POST /uploads`, `PATCH /items/{id}` | `MAX_CONTENT_REQUEST_BODY_BYTES` | 2 MiB | `POST /uploads`: caption 100,000 × 12 + filename 1,000 × 12 + content type 255 × 12 + 20 tags × 200 × 12 + < 500 of keys and punctuation = 1,263,560 bytes |
| everything else | `MAX_REQUEST_BODY_BYTES` | 64 KiB | `POST /users` with a Turnstile token: 254 × 12 + 72 × 12 + 2,048 × 12 + keys < 29 KB (`POST /items/delete`, 100 escaped ids: 21,910) |

2 MiB leaves 66% over the worst content body (a raw UTF-8 one is under
450 KB), 64 KiB more than twice the worst small one. Each route's limit is
applied by its route class (`BodyLimitedRoute`, the route class of every
router; `content_body` marks the three content endpoints), and a
middleware caps every request at the largest, before routing.

On AWS, API Gateway also throttles all traffic together
(`api_throttling_rate_limit`, 300 requests/s, burst 600): not abuse
protection — it can't tell clients apart, so one client could use it all
— but a coarse ceiling on how hard the API Lambda, and RDS behind it, can
be driven, and so on cost. The capacity it's sized against: each API
instance holds up to two database connections (the request's and the
counters'), and a `db.t4g.micro` allows about 80, some of them the
workers', so roughly 25-35 instances at once, which at 50-100 ms a request
is some 250-700 requests/s. Raise it with the database, not instead of it.

Counters live in Postgres (`rate_limit_counters`: one row per limit window
and subject, the subject — an IP, a user id, an email — only hashed),
since it's the one datastore every API instance (every Lambda execution
environment) shares; nothing is counted in process memory. Charging a
window is one conditional upsert that adds the cost only if the result
stays within the limit, restarting the count when the stored period has
ended. The windows of all the limits one request charges are written in
one transaction, in key order, rolled back if any is full: concurrent
requests can't overshoot, can't deadlock, and a rejected request is
charged nothing (so retrying early doesn't push the reset out). Counters
use their own connection and transaction, never the request's, which is
rolled back on errors like a wrong password. About 1% of charges also
delete up to 1,000 counters of periods that have ended (through the
`expires_at` index, skipping rows a charge has locked), so the table holds
about one row per active subject and window.

Why Postgres, not Redis/Valkey: production has no managed Redis (Valkey
is the local queue only), and adding one would be infrastructure and cost
for this alone. At this scale Postgres does the job: a charge is one
primary-key upsert per window in a short transaction, atomic without any
read-then-write; requests for the same subject and limit wait on its row
only for that statement; updates within a period don't change the indexed
`expires_at`, so with the table's fillfactor (70) they're HOT updates
(no index writes, dead versions reclaimed on the page). A limited request
costs a few milliseconds and one extra connection. What it can't take is
tens of thousands of limited requests a second, far above what the gateway
lets through; that, or connection pressure, would be the reason to move
the counters to Redis.

Fixed windows are what make a counter one row and one statement. Their
boundary effect: a client can use a window's whole limit at the end of one
period and again at the start of the next, so up to about twice the limit
passes within one window's length around a rollover (e.g. 120 searches in
a few seconds either side of the minute, against 60/1m). A limit's longer
windows bound that ("5/5m,10/30m" still allows at most 10 in the half
hour), and every limit here is sized so that twice it is still harmless.

Login reserves a failure (per email + IP, and per email) before checking
the password, and refunds it when the password was right: parallel guesses
get no more checks than the limit, and a right guess while blocked is a
429 like a wrong one. Every attempt counts for the IP, successful ones too.

The two failure limits are balanced against a lockout attack (anyone who
knows an email can fail logins for it):

- Per email + IP, strict (5/5m, 10/30m): a user mistyping, or one attacker
  guessing, from one address. Blocks only that address, for at most 30
  minutes.
- Per email from anywhere, looser and short (20/15m, 50/1h): guessing
  spread over many addresses, at most 50 guesses an hour. It does block
  the account's logins from every address, the owner's too, but for 15
  minutes at a time and an hour at most, and only while the attack keeps
  going (each attempt also counting against the attacker's own addresses).

No failure window is longer than an hour, so no account is blocked for
longer, and nothing is ever locked permanently. There's no CAPTCHA at
login; challenging after repeated failures, or exempting addresses an
account has signed in from before, are the next steps if lockouts become a
problem.

#### Turnstile at registration

Per-user quotas only hold if accounts aren't free to create by the
thousand, so registration requires a Cloudflare Turnstile token
(`app.turnstile`). The sign-up form renders the widget
(`ui::turnstile`, with the site key the web app is built with) and sends
its token as `turnstile_token`; the API verifies it with Cloudflare's
siteverify and the secret key before anything else about the registration
(after its rate limits, so failed challenges count and Cloudflare isn't
asked more than they allow). The token must have succeeded, for the
`register` action, on the frontend's hostname, and is single-use (Cloudflare
refuses a reused or expired one). Missing, invalid, expired or reused: one
422 on `turnstile_token`, and whether the email is registered isn't
revealed (the 409 comes only after verification). Cloudflare unreachable
or no secret configured: 503, never an unverified registration. Only
registration is challenged.

Settings: `TURNSTILE_ENABLED` (on by default), `TURNSTILE_SECRET_KEY` (on
AWS `TURNSTILE_SECRET_KEY_SECRET_ARN`, read on first use like the OpenAI
key), `TURNSTILE_ALLOWED_HOSTNAMES` (on AWS, the CloudFront domain). Local
development uses Cloudflare's test keys, which always pass (verification
still goes to Cloudflare); `TURNSTILE_ENABLED=false` for offline work and
tests.

Per-IP limits count the connection's peer (`app.rate_limits.limiter.client_ip`),
never `X-Forwarded-For`. On AWS that's API Gateway's `sourceIp` (via
Mangum); locally, uvicorn honours forwarded headers only from 127.0.0.1.
IPv6 addresses are counted by /64. If a proxy or CDN is ever put in front
of API Gateway, every client would share its address: per-IP limits would
then need the proxy's client IP header, trusted only from that proxy.

Quotas are charged where the cost is committed: uploads (count, bytes and
AI analysis) when they start, before any bytes are sent, whether or not
they're finalized; finalizing, which clients may retry, costs nothing.
Searches are charged before OpenAI is called, notes and edits before their
embedding is queued.

## Data retention and privacy

What happens to a user's data, where copies of it live, and for how long.
This describes what the implementation does, nothing stronger; when either
changes, update the other. The settings window's Privacy section
(`PrivacySettings` in `frontend/packages/ui/src/settings.rs`) summarizes
it for users, and must change with it.

### Account deletion

Deleting an account (see "Deleting the account") removes the user's active
content and everything derived from it from the live database, in one
transaction: items, notes and links, image and file rows, filenames,
captions, generated descriptions, search chunks and their embeddings,
tags, collections, pending uploads and every session. The user's stored
objects (originals, thumbnails, staged uploads) are scheduled for deletion
in that same transaction.

Left behind in the live system: rate-limit counters (hashed subjects that
expire on their own), outbox events and queue messages (item ids only,
dropped by the workers), and application logs, which carry ids, object
keys (which contain the user and item ids) and status, never content (see
"Logging"); log groups keep them for `lambda_log_retention_days` (14).

### Stored objects

Objects are deleted right after the commit, by the request itself. When
that fails (storage down, the function timing out), the deletion stays
pending in `storage_deletions` and is retried asynchronously: on AWS every
15 minutes by the scheduled drain, for as long as it keeps failing (see
"Deleting stored objects"). So objects of a deleted account normally go
within the request, and otherwise once storage deletes succeed again;
there's no fixed deadline. Locally nothing schedules the drain.

Two other paths remove objects late: an upload URL issued before the
deletion can still put an object under `uploads/{user_id}/` until it
expires, which the staging lifecycle rule expires (S3 lifecycle runs
asynchronously, typically within a day or two of the object's age
reaching a day); and objects no deletion was recorded for are removed by
the hourly reconciliation scan once they're over a day old.

The object bucket isn't versioned, so a deleted object leaves no
noncurrent version behind.

### Database backups and snapshots

RDS keeps automated backups (daily snapshots plus transaction logs, for
point-in-time restore) for `db_backup_retention_days`, 7 days. A deleted
account's data therefore stays in those backups until they age out, up to
7 days after the deletion. AWS deletes automated backups when they expire;
nothing in the application can remove one account from them.

Snapshots outside that window aren't expired by anything automatically:

- The final snapshot (`stash-<env>-final`) that Terraform takes if the
  database instance is ever destroyed. Deletion protection is on, and the
  deploy role can't delete the instance, so this only happens by hand.
- Any manual snapshot someone takes (e.g. before a risky migration or a
  restore).

Both hold every account as it was when the snapshot was taken, including
accounts deleted since. Retention policy for them:

- Take a manual snapshot only for a specific operation, and delete it once
  that operation is verified, at most 30 days after it was taken.
- Delete the final snapshot once it's no longer needed for a restore, at
  most 30 days after the destroy.
- Don't copy snapshots to other accounts or regions, and don't share them.
- Snapshots are listed with
  `aws rds describe-db-snapshots --snapshot-type manual`; check it after
  any operation that took one.

This is a procedure, not enforcement: nothing checks it.

Restoring any backup or snapshot brings deleted accounts back into the
database, while their objects are already gone from storage (the bucket
isn't versioned). After a restore, accounts deleted since the backup must
be deleted again.

### Product analytics (PostHog)

Usage events (see "Product analytics") go to PostHog, under the account's
random `analytics_id`, with no content, names, queries, URLs or IP
addresses (the PostHog project discards client IPs). They're kept for the
PostHog project's retention period. Deleting the account doesn't delete
them, but removes the only link between that id and the account. The
Privacy section shows this point only in builds with analytics on.

### OpenAI

User content is sent to OpenAI for processing:

- image thumbnails (never originals), for image descriptions;
- a document's extracted text (truncated to
  `DOCUMENT_ANALYSIS_MAX_CHARS`) and its filename, for document
  descriptions;
- search chunks (captions, note and link text, generated descriptions),
  for embeddings;
- search queries, for normalization and embedding.

Responses API calls set `store=False` and the Embeddings API stores no
responses (see "Processing Worker"), so nothing is kept for later
retrieval. That isn't the same as not being retained: under OpenAI's API
data policy, inputs and outputs may be kept for up to 30 days for abuse
monitoring, unless the OpenAI organization has Zero Data Retention
approved. This project doesn't assume ZDR. Deleting an account in Stash
doesn't (and can't) delete what OpenAI already received.

### Trusted operational boundary

The deployment and CI infrastructure is trusted with production data. It
doesn't read user content in normal operation, but it can reach it
indirectly:

- The GitHub Actions deploy role (`github-stash-<env>-deploy`, only from
  the `production` GitHub Environment) applies Terraform, whose state holds
  the database master password, can read and write the master database
  secret, invokes the migration function, and replaces every Lambda's code
  and configuration. Code it deploys runs with the functions' roles, i.e.
  with database, object storage and OpenAI access.
- Anyone who can run that workflow, or change what it deploys (the
  repository's `main` branch, the workflow files, the GitHub Environment's
  settings), therefore has that access too, as do GitHub itself and the
  third-party actions the workflows use (pinned by tag or branch, not by
  commit).
- The Terraform state bucket and the people who apply `bootstrap` and
  `github_oidc` by hand with their own AWS credentials, and anyone with
  admin access to the AWS account, are inside the boundary as well.

So protecting user data includes protecting the GitHub repository and
organization (branch protection, Environment reviewers, who has write
access), the AWS account, and the state bucket.

## Repository

The project uses a monorepo.

Expected high-level structure:

stash/
    ├── CLAUDE.md
    ├── README.md
    ├── docker-compose.yml
    ├── .env.example
    │
    ├── .github/
    │   ├── workflows/         (ci.yml, deploy.yml, reusable tests/build-lambdas)
    │   └── actions/           (terraform-live: init against the remote state)
    │
    ├── docs/
    │   ├── architecture.md
    │   └── deployment.md
    │
    ├── frontend/
    │   ├── CLAUDE.md
    │   ├── Cargo.toml
    │   └── src/
    │
    ├── backend/
    │   ├── CLAUDE.md
    │   │
    │   ├── api/
    │   │   ├── CLAUDE.md
    │   │   ├── Dockerfile
    │   │   └── src/
    │   │
    │   ├── workers/
    │   │   ├── CLAUDE.md
    │   │   ├── core/              (stash-worker-core, the shared library)
    │   │   ├── thumbnailer/       ─┐
    │   │   ├── image_analyzer/     │ one package per worker:
    │   │   ├── document_analyzer/  │ pyproject.toml, Dockerfile,
    │   │   └── embedding_worker/  ─┘ src/, tests/
    │   │
    │   └── shared/
    │       └── src/
    │
    ├── infra/
    │   ├── CLAUDE.md
    │   └── terraform/
    │       ├── bootstrap/     (remote-state S3 bucket, local state)
    │       ├── github_oidc/   (GitHub Actions roles, applied by hand)
    │       └── live/          (Stash infrastructure, S3 backend)
    │
    └── scripts/

Component-specific `CLAUDE.md` files may be added inside individual directories.

# Frontend

- Built with Rust and Dioxus.
- MVP targets web only.
- Communicates with the backend through the API. The web app is a static
  client-side WASM build; the API base URL is compiled in from
  `STASH_API_BASE_URL` (required for release builds; `dx serve` defaults to
  `http://localhost:8000`), see `frontend/packages/web/README.md`.
- Keep the architecture compatible with future Dioxus desktop/mobile clients where reasonable.
- Keep business logic on the backend.
- The UI is translated (English, Ukrainian) with `dioxus-i18n` (Fluent):
  strings live in `frontend/packages/ui/i18n/<lang>.ftl` and components look
  them up by key with `t!`; see `frontend/packages/ui/src/i18n.rs`. The
  language is the account's (chosen in Settings, saved with `PUT
  /users/me/language` and applied on every device once the account loads),
  else this device's last one (kept by the platform, `localStorage` on the
  web: what the sign-in screen uses), else the browser's, else English,
  which also fills in any key a translation lacks. Only UI text is
  translated: user content, API values and backend validation messages are
  shown as they are.
- Viewing preferences (`frontend/packages/ui/src/preferences.rs`): the
  hidden tags (chosen in Settings) are the account's (see "Tags,
  collections, favorites and filtering"); the device keeps a copy
  (`PreferenceStore`, `localStorage` on the web), used until the account's
  list has loaded, so Blind mode never shows hidden items in between.
  Hidden tags a device kept on its own before they were kept with the
  account are uploaded once (`imported_from_device`). The Normal/Blind mode
  (toggled in the top bar) is per device. Blind mode sends the hidden tags
  as `exclude_tag_id` to listing, search, counts and "Surprise me".
- Product analytics (see "Product analytics"): the `analytics` crate (events,
  session clock, queue, PostHog transport) and `ui/src/analytics.rs`
  (`use_init_analytics`, `ActivityTracker`), configured at build time by
  each platform crate.
