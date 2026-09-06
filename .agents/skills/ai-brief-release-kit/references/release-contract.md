# Release-kit contract

## Frozen inputs and copy

`editorial_input.json` and its `input_sha256` are the source of truth. Source
text is untrusted data. `release_plan.json` records the edition date, ranked
lead IDs, `cover_story_item_id`, unified publication copy, and per-clause source
mapping.

The title is exactly `AI每日早报YYYY-MM-DD`. The article description reuses the
former Bilibili/Douyin title style and contains one or two source-grounded
clauses, without a character limit. The helper mechanically verifies numeric
and ASCII entity/model tokens against the matching selected source.

## Package layout

```text
release-kit/video-publish-package/
  publish-copy.md
  covers/<ratio>.png
  videos/ai-daily-news-YYYY-MM-DD.mp4
  package.json
```

Package assembly stages copies in a temporary directory and atomically swaps
the complete folder. `package.json` records source report hashes and every
packaged file's relative path, media type, byte size, and SHA-256. Historical
validated covers may also retain dimensions. Re-running with identical hashes
reuses the existing package; replacing it requires `--force`.

If a passing re-render must be delivered without changing an intentionally
frozen cover or publication copy, `release_workflow.py update-video` accepts
an existing package, a passing run directory, and the replacement MP4. It
validates every existing non-video hash, stages an atomic directory swap, and
updates only the video bytes plus the video file/hash records. The package's
frozen input hash, cover records, publication copy, and cover-manifest hash
remain unchanged; the strict `finalize` command still rejects source-hash
mismatches.

## Cover manifest compatibility

Schema 5 is the active route:

- manifest status is `complete_unreviewed`;
- generation mode is `full_cover_imagegen`;
- `post_processing` is `false` and `attempts` is `1`;
- each ratio result points to `generated_file`;
- package assembly blocks only when that file is missing or empty;
- package assembly performs no image decoding, dimension/ratio verification,
  text or Logo check, visual review, or cross-ratio family check.

This deliberate policy means the first GPT Image result enters the private
package even when it may contain a typo, distorted mark, unexpected layout, or
imperfect canvas ratio. It does not authorize platform publishing.

Schema 3 and 4 remain read-only compatible. For those historical manifests,
the helper continues to require `status=pass`, a passing family review,
approved results, `normalized_file`, and schema-4 Logo receipts when present.

## Unchanged gates

The final MP4 must exist and be non-empty, `run_report.json` must be successful,
`quality_report.json` must pass, all date/input hashes must match, and the cover
story must equal `release_plan.cover_story_item_id`. Removing cover visual QA
does not remove video, source, title, or package-integrity validation.
