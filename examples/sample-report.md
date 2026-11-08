# Harbor Notes: weekly engineering report, week of 2 November 2026

All names and figures in this report are fictional. Use it to try Local Delegate.

## Summary

Sync v2 reached feature freeze on 4 November. Export and search are stable. Two release blockers
remain: a sync conflict bug that drops one of two edited notes, and a retry test that errors on
Windows. The 2.4 release moves from 10 November to 17 November.

## Done this week

- Sync v2: delta uploads, conflict detection and the new retry queue (pull requests 412, 418, 421).
- Search: prefix matching for tags. The median query time fell from 38 ms to 21 ms on the
  10,000-note test set.
- Export: Markdown export now includes tags and the created date.
- Accessibility: screen-reader labels on the editor toolbar, 12 of 14 controls done.

## In progress

- Conflict handling: when both copies change, sync must keep both and mark the newer one. Today it
  keeps only the newer copy (issue 430). Owner: Priya. Due 9 November.
- Retry tests: `test_sync_retry` errors because the fake server no longer accepts a `timeout`
  argument (issue 431). Owner: Marco. Due 6 November.
- Export dates: the created date drops the time of day (issue 433). Owner: Lena. Due 12 November.

## Risks

| Risk | Likelihood | Impact | Plan |
|---|---|---|---|
| The conflict fix slips past 9 November | Medium | The release moves again | Pair on it Monday; ship "keep both" without the newer-copy badge |
| App store review takes longer than 3 days | Low | Release day moves | Submit the build by 13 November |
| The search index rebuild is slow on old phones | Medium | A 20-second spinner after updating | Rebuild in the background, behind a flag |

## Decisions

- 3 November: the 2.4 release moves to 17 November (product and engineering leads).
- 4 November: feature freeze for sync v2; only bug fixes merge until the release.
- 5 November: the newer-copy badge is optional for 2.4.

## Metrics

| Metric | Last week | This week |
|---|---|---|
| Open bugs | 37 | 31 |
| Crash-free sessions | 99.2% | 99.4% |
| Test suite | 44 tests, all passing | 2 failed, 1 error |
| Median sync time | 1.9 s | 1.4 s |

## Next week

1. Fix the conflict bug (issue 430) and the retry test (issue 431).
2. Run the release checklist on 12 November.
3. Submit the 2.4 build to the app stores by 13 November.
4. Release on 17 November if the checklist passes.

## Asks

- Design: the final icon for the "both copies kept" notice, by 8 November.
- Support: a help-center article on resolving sync conflicts, by 15 November.
