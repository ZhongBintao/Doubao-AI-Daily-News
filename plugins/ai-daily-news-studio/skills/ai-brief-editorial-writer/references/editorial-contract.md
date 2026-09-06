# Editorial writer v4 / plan v5 contract

- Every selected source item appears exactly once. Every generated field cites
  exact `claim_ids`; overview copy includes at least one `summary` or `detail`
  claim.
- Each story names `subject`, `navigation_title`, `overview_text`,
  `overview_claim_ids`, and a unique `presentation_order`.
- Navigation labels are complete “主体＋具体变化” phrases. Reject bare labels
  such as “事件解读”“引争议”“最新动态”“安全” and all ellipses.
- Every beat has a stable `beat_id`, terminal punctuation, and one or more
  claims. Beats may contain multiple sentences and have no fixed character or
  duration limit. Impact, action, and limitation are optional unless supported.
- Subtitle units are generated after writing and stay within 28 visible units;
  they inherit the beat ID and claim context without deleting narration.
- Cards have stable IDs and subjects. Their number and body length are driven
  by evidence; the renderer paginates them and never executes `cards[:4]`.
- `visual_asset_id` may appear on later beats after a card-first beat. X stories
  may use one asset; ordinary web stories may use the first-viewport asset and
  at most one article-image asset. Each selected schema-5 asset must share a
  claim with its beat.
- Display numbers omit thousands separators. `normalize_with_ledger` alone
  generates spoken text and pronunciation records.
- `writer.status=approved` is required; the finalizer changes it to
  `finalized`. Historical plan v2–v4 is read-only compatible, but new production
  runs must use v5.
