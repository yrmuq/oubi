## Required Behavior

- Keep the container source mount at `/images` unless the same change migrates existing `tags.csv` and `sorted.txt` paths, which store absolute container paths.
- `Sort` and `Sort plan` must work from an existing `tags.csv` even when the PixAI model has not finished downloading.
- Show source folders immediately while scan and sort status counts are computed in the background, so a large image collection does not delay the folder list.
- Build filter suggestions from all tagged files in the selected folders, including files outside the recent-image preview.
- Keep numbered pages of a series together in the destination and preserve their page numbers.
- Apply genre folders only to `original` images and use the first matching configured genre so the order chosen in the UI decides ties.
- Keep the genre add and remove buttons together in one field-width slot so a shorter row does not stretch its numbered fields.
- Keep GPU runtime downloads opt-in and separate from the CPU Python environment so standard Docker pulls and CPU tagging never require ROCm or CUDA packages.
- Prefer a ready installed GPU at page startup and honor an explicit manual device selection while the device is available.
- In the web interface, show the GPU bundle and its estimated download size in the shared terminal before starting a download. Accept `y` or `n` there for the specific pending installation request.

- Read and write image tag sidecars only in the image directory's `tags/` subfolder, using the full image filename plus `.txt` to distinguish images with the same stem.
- Preview deletion applies only to the confirmed image in its original workspace and removes only that image's tag sidecar and index entries.
- Keep status polling independent of vault filesystem requests so the terminal can report a slow unlock or network scan.
- Release the Cryptomator lock before sending file and folder responses so a slow or disconnected browser cannot block other reads or page reloads.
- Store the vault's rebuildable SQLite index inside Cryptomator and execute queries in memory for the open session, avoiding SQLite file locking on the NAS.
- Update the vault index for files changed through Oubi and use the recount control to reconcile edits made outside Oubi.
- Generate vault thumbnails on demand inside Cryptomator, load the original on zoom, and remove cached previews when Oubi deletes the source or a recount finds it missing.
- List each folder's tag sidecars once during a vault index rebuild to avoid a separate NAS lookup for every missing sidecar.
- Keep the sort history open during a sort and flush completed entries in batches and when a graceful stop unwinds the job.
- Keep the gallery selection separate from the latest completed tagging result so switching preview modes leaves the running job and gallery position intact.
- Clear active tag filters on return from Gallery to Sorting, keeping favorite tags persistent per workspace and inside Cryptomator for an encrypted workspace.
- Keep Gallery on the browsable image preview while a job runs and hide the sorting controls, sorting badges, and preview mode switch.
- Order folder names by symbols, numbers, then letters by default, comparing digit sequences numerically.
- Put tags from the CSV character column under Characters and all remaining favorites under Events.
- Display the decoded preview and its filename together before requesting image tags so metadata reads do not delay the current frame.
- Keep Cryptomator indexes, sort history, and generated metadata inside the vault; expose the unlocked workspace only to the browser session that unlocked it.
- Resolve the internal sort destination relative to the Cryptomator mount and exclude its subtree from vault source operations.
- Ship Cryptomator in the shared Oubi image so operators can install or update vault-enabled deployments with Compose pulls.

## Compatibility Workarounds

- Retry the unauthenticated GHCR pull after a successful push because the new manifest can take time to become visible. Remove the retry only when pulls can reliably see the just-pushed manifest.

## External Compatibility Constraints

- Keep a named account for the default Compose UID in the image because `fusermount3` resolves the numeric UID before mounting the vault.
