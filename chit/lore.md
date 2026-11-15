## Required Behavior

- Keep the container source mount at `/images` unless the same change migrates existing `tags.csv` and `sorted.txt` paths, which store absolute container paths.
- `Sort` and `Sort plan` must work from an existing `tags.csv` even when the PixAI model has not finished downloading.
- Show source folders immediately while scan and sort status counts are computed in the background, so a large image collection does not delay the folder list.
- Build filter suggestions from all tagged files in the selected folders, including files outside the recent-image preview.
- Keep numbered pages of a series together in the destination and preserve their page numbers.
- Apply genre folders only to `original` images and use the first matching configured genre so the order chosen in the UI decides ties.
- Keep the genre add and remove buttons together in one field-width slot so a shorter row does not stretch its numbered fields.
- Keep AMD runtime downloads opt-in and separate from the CPU Python environment so standard Docker pulls and CPU tagging never require ROCm packages.

## Compatibility Workarounds

- Retry the unauthenticated GHCR pull after a successful push because the new manifest can take time to become visible. Remove the retry only when pulls can reliably see the just-pushed manifest.
