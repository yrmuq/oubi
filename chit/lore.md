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
- In the web interface, show the GPU bundle and its estimated download size in the shared terminal before starting a download. Accept `y` or `n` there for the specific pending installation or removal request.
- Protect GPU runtime packages while a job uses them and retain a shared CUDA build while another card remains configured for it.

- Read and write image tag sidecars only in the image directory's `tags/` subfolder, using the full image filename plus `.txt` to distinguish images with the same stem.
- Image deletion applies only to the confirmed file revision in its original workspace and removes only that image's tag sidecar and index entries.
- Deleting an image from its browser tile works even when its contents cannot be decoded.
- Keep status polling independent of vault filesystem requests so the terminal can report a slow unlock or network scan.
- Release the Cryptomator lock before sending file and folder responses so a slow or disconnected browser cannot block other reads or page reloads.
- Store the vault's rebuildable SQLite index inside Cryptomator and execute queries in memory for the open session, avoiding SQLite file locking on the NAS.
- Update the vault index for files changed through Oubi and use the recount control to reconcile edits made outside Oubi.
- Generate vault thumbnails on demand inside Cryptomator, load the original on zoom, and remove cached previews when Oubi deletes the source or a recount finds it missing.
- Share the JPEG XL decoder between the web process and existing GPU environments. Serve PNG to clients that omit native JPEG XL support and retain the original format when sorting.
- List each folder's tag sidecars once during a vault index rebuild to avoid a separate NAS lookup for every missing sidecar.
- Keep the sort history open during a sort and flush completed entries in batches and when a graceful stop unwinds the job.
- Keep the gallery selection separate from the latest completed tagging result so switching preview modes leaves the running job and gallery position intact.
- Clear active tag filters on return from Gallery to Sorting, keeping favorite tags persistent per workspace and inside Cryptomator for an encrypted workspace.
- Keep the preview mode switch in both Sorting and Gallery so users can browse images during a tagging job.
- Keep the tag search and active filters beneath the preview in the default layout, with genre tags or favorites beneath the current image tags according to the active mode.
- Merge imported favorites into the active workspace without case-insensitive duplicates and preserve any existing tag's group.
- Show paths relative to the library root in the tagging progress label while keeping absolute paths in image metadata.
- Report images found during the Identify walk and tag checks against the final count before tagging begins, independently of the vault index scan.
- Keep folders before files in both browser order modes, with untagged images first by default and names compared by symbols, numbers, then letters using numeric digit sequences.
- A folder with no subfolders remains selectable with the square selection button while the grid shows only its image files.
- Show only the current folder's image files in the browser and use the open vault's index to display their tagging status without loading image data.
- Put tags from the CSV character column under Characters and all remaining favorites under Events.
- Display the decoded preview and its filename together before requesting image tags so metadata reads do not delay the current frame.
- Keep Cryptomator indexes, sort history, and generated metadata inside the vault; expose the unlocked workspace only to the browser session that unlocked it.
- Review duplicate matches as direct pHash pairs and remember rejected pairs only for the file revisions reviewed.
- Verify both files again before deleting a duplicate so a stale comparison cannot remove the only remaining copy.
- Stage both duplicate files and their metadata in one removal so a failure on the second file restores the first.
- Run preview, tile, and duplicate removals in the same FIFO queue per workspace and hide reserved files while the queue runs so slow file operations do not stop browsing or review.
- Pause the vault deletion queue before unmounting and resume its pending work only for the same vault after unlocking.
- Use the configured Cryptomator start folder as the initial browser location without changing the vault scan root, output paths, or folder selection for jobs.
- Resolve the internal sort destination relative to the Cryptomator mount and exclude its subtree from vault source operations.
- Ship Cryptomator in the shared Oubi image so operators can install or update vault-enabled deployments with Compose pulls.
- Enable vault controls at startup when `/crypt/vault.cryptomator` is present. The container path is fixed so operators only configure the host bind mount.
- Return vault unlock errors to the requesting browser with the failed stage and a concise Cryptomator or FUSE diagnostic, redacting the submitted password.
- Open the Cryptomator password form outside the native modal top layer so browser password managers can display their inline autofill controls.
- Run scheduled jobs in a separate workspace context. A scheduled vault mount waits for the manual session to close and never grants browser access to the unlocked vault.
- Protect scheduled Cryptomator passwords with both TPM 2.0 and an automatically generated host key. Deletion decisions remain manual.
- Give the Alpine TPM service the device under its root ownership so scheduled unlock works with a `0600` host TPM without changing host permissions.
- Keep the TPM host key volume private to the Alpine service and authenticate requests using the UID supplied by the Unix socket.
- Preserve existing direct TPM credentials for installations using their original device mapping.
- Use textareas for the schedule folder and cron so Bitwarden's username heuristic skips them while its password field remains available.

## Compatibility Workarounds

- Retry the unauthenticated GHCR pull after a successful push because the new manifest can take time to become visible. Remove the retry only when pulls can reliably see the just-pushed manifest.

## External Compatibility Constraints

- Keep a named account for the default Compose UID in the image because `fusermount3` resolves the numeric UID before mounting the vault.
- Mount the unlocked vault under `/mnt` inside the container because Ubuntu's `fusermount3` profile permits FUSE mounts there.
- Write vault metadata using the original absolute prefix `/run/oubi-crypt/mount` so deployments on older Oubi images can read the same vault.
