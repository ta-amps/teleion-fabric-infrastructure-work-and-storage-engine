# Raw blob governance and the first uploader

Status on 2026-10-30, branch `t/blob-first-uploader-20261030`, not merged or deployed. This
note covers the open finding in the [review findings log](review.md#findings-log): raw blob
privacy is set by whoever uploads the bytes first in a namespace.

## How it works

A raw blob is stored once per digest. Its governance (visibility, producer surface, allowed
surfaces, cohorts, tags, owner) is one `blob_namespaces` row per namespace and digest, written
with `ON CONFLICT (namespace, sha256) DO NOTHING` by the Worker (`recordBlobStored` and
`putObjectMeta` in `deploy/cloudflare/cloud_store.js`) and by Python (`_store_blob_in_tx` in
`src/tessera/_storage.py`). Bucket object uploads write that row too, with the object's
governance. `GET /v1/blobs/{sha256}` checks only that row. The Worker's D1 `blob_namespaces`
projection (`storeCloudBlob`) is written the same way but authorizes nothing.

## Fixed on this branch

**A key could not read back bytes it uploaded.** Key B uploads bytes that key A already holds
privately in the namespace, as a raw blob or as a private object. B gets `201`, then `404` on
`GET /v1/blobs/{sha256}`. B loses access to its own upload, and the `201`/`404` pair tells B that
someone holds those bytes privately there. Both runtimes did this.

Now both runtimes record each uploading key in a `blob_uploaders` table (namespace, digest,
key). A raw read passes when the first uploader's row admits the caller or when the caller's key
uploaded those bytes into the namespace itself. Uploading grants no access to anything the key did
not send, and other keys' access is unchanged. On the Worker the table is created by a
namespace's first upload after deploy, so a cold start stays read-only. Python creates it when the
database opens, as for every additive table.

**On Python, the first uploader also governed bucket objects.** `get_object` re-applied the raw
blob row to the object's bytes. If A had stored the bytes privately first, B's public object
holding them was unreadable to everyone except A, including B. The review's note that bucket
objects are not affected was true only on the Worker. Python object reads now follow the
object's own ACL, as they do on the Worker. Quarantine checks are unchanged.

Reproductions, each failing on main `33db2d4`:

- `tests/test_blob_first_uploader.py::test_a_later_uploader_reads_back_bytes_another_key_stored_privately`
  (Python over HTTP: `201` then `404`).
- `tests/test_blob_first_uploader.py::test_an_object_is_governed_by_its_acl_not_by_who_stored_its_bytes_first`
  (Python store: `NotFound` for the object's own writer).
- `tests/workerd_blob_first_uploader.mjs` (the real Worker: a raw blob after a private blob, and
  after a private object).

`test_quarantine_and_private_objects_still_hide_bytes_from_keys_that_never_held_them` checks that a
key that never held the bytes still cannot read them.

## Still open: these need a decision

1. **A later upload does not change what other keys can read.** If A's bytes are public in the
   namespace, B's private upload of the same bytes leaves them public, and B is not told. Nothing
   new is exposed, because the bytes were already readable there. In the other direction, B's
   public upload does not make bytes that A holds privately readable to anyone but B. The spec
   says storing the same bytes again is a no-op (`conformance-draft/SPEC.md` §6). Options: one
   governance row per uploader, with a read allowed when any of the rows admits the caller (this
   changes the spec's no-op wording); or returning the effective visibility in the upload
   response.
2. **The Worker's public board shows the namespace's highest event number.** `blob_stored` is
   appended only when the bytes are new to the namespace, so comparing the board's "Event cursor"
   before and after an upload in a quiet namespace tells whether the bytes were already there.
   Python's board takes its cursor from public events only. On both runtimes, though, a gap
   between the numbers of consecutive public events still counts the events in between. Options:
   append `blob_stored` for each new uploader rather than for new bytes (this changes what the
   event means), or show no event numbers on public pages.
3. **Upload time (not measured).** The Worker's D1 `blobs` table is shared by every namespace,
   and `storeCloudBlob` writes chunks only for bytes new to the deployment. An upload of bytes
   already stored in any namespace can therefore return faster. This comes from deduplicating
   across namespaces. It is closed only by always writing, or by keying stored bytes per
   namespace.
4. **Python hides bytes by quarantine across objects.** On Python, an object tagged
   `status:quarantined` hides raw reads and object reads of the same bytes from keys outside the
   quarantining surface, including keys that uploaded the bytes themselves. A non-admin key may
   tag its own objects. So one key can hide content it can guess from other keys in the
   namespace, and a reader can learn that some object holding those bytes is quarantined. The
   Worker applies quarantine per object. This needs a decision on whether quarantine covers one
   object or all copies of its content, and who may set it.
5. **Low: the Worker's blob tags.** Each raw upload replaces the blob's `noun_tags` with the latest
   uploader's tags, while the governance row keeps the first uploader's. No query reads blob tags
   today (`nounFilterSql` covers records and objects only), so nothing visible changes.
6. **Anonymous retention leaves blob and object rows.** Python's `purge_expired_principals`
   removes an anonymous namespace's tasks, records, events and executions, but not its
   `blob_namespaces`, `blob_uploaders`, buckets or objects. The Worker clears the namespace's
   Durable Object storage, including `blob_uploaders`, but nothing removes that namespace's rows
   from the D1 projections, or its bytes from the shared D1 or R2 store.

Until these are decided, keep private material in private bucket objects or records rather than
raw blobs.

## A full fix, if chosen

- Add governance columns to `blob_uploaders`, making it one governance row per uploader, and allow
  a raw read when the `blob_namespaces` row or any uploader row admits the caller. Keep the
  `blob_namespaces` rows, because older blobs have no uploader rows. Uploads that were dropped by
  `DO NOTHING` before this branch cannot be backfilled: their uploading key was never recorded.
- Append `blob_stored` when a new uploader row is written, and take public cursors and event
  numbers from public events only.
- Decide the quarantine scope and who may set it, then make both runtimes agree.
- Update `conformance-draft/SPEC.md` §6, in a task that says so, and add suite tests for the
  agreed rules on both runtimes.
