#!/usr/bin/env bash
set -euo pipefail

repository=${GITHUB_REPOSITORY:?}
channel=${CHANNEL:?}
image=${IMAGE:?}

branch_head() {
    git ls-remote --exit-code --heads origin "refs/heads/$1" | cut -f 1
}

lookup_digest() {
    local result digest
    if result=$(docker buildx imagetools inspect "$1" 2>&1); then
        digest=$(awk '$1 == "Digest:" {print $2; exit}' <<<"$result")
        if [[ $digest =~ ^sha256:[[:xdigit:]]{64}$ ]]; then
            printf '%s\n' "$digest"
            return
        fi
    elif grep -Eiq 'manifest unknown|name unknown|not found' <<<"$result"; then
        return 1
    fi
    printf '%s\n' "$result" >&2
    return 2
}

prepare() {
    local base source version ref available published tag candidate status version_commit dev_parent extra_parent parents
    local -a candidates=() tags=() tpm_tags=() registry_versions=()
    base=$(git rev-parse HEAD)
    source=$base
    version=$(tr -d '[:space:]' < VERSION)

    case $channel in
        dev)
            ref=$(branch_head dev)
            [[ $ref == "$base" ]] || return 0
            candidates=("sha-$source")
            ;;
        stable)
            [[ $GITHUB_REF == refs/heads/main && $base == "$GITHUB_SHA" ]]
            parents=$(git show -s --format=%P "$base")
            read -r _ dev_parent extra_parent <<<"$parents"
            if [[ -z $dev_parent || -n $extra_parent ]]; then
                echo "Stable publication requires a dev merge on main." >&2
                return 1
            fi
            git fetch origin dev
            git merge-base --is-ancestor "$dev_parent" FETCH_HEAD || {
                echo "The merged commit must belong to dev." >&2
                return 1
            }
            version=$(sh scripts/bump-version.sh stable VERSION)
            git config user.name 'github-actions[bot]'
            git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
            printf '%s\n' "$version" > VERSION
            git add VERSION
            # The same source and version must produce the same release commit on a retry.
            version_commit=$(git show -s --format=%cI "$base")
            GIT_AUTHOR_DATE=$version_commit GIT_COMMITTER_DATE=$version_commit \
                git commit -m "Add version $version" -m '[release]'
            source=$(git rev-parse HEAD)
            ref=$(branch_head main)
            [[ $ref == "$base" || $ref == "$source" ]]
            if [[ $ref == "$base" ]]; then
                if git ls-remote --exit-code --heads origin refs/heads/deps >/dev/null; then
                    git fetch origin deps
                    git merge-base --is-ancestor FETCH_HEAD "$base" || {
                        echo "Merge the pending deps changes into dev and test before publishing." >&2
                        return 1
                    }
                elif [[ $? -ne 2 ]]; then
                    return 1
                fi
            fi
            candidates=("sha-$source")
            ;;
        deps)
            ref=$(branch_head deps)
            [[ $ref == "$base" ]] || return 0
            git fetch origin --tags
            git fetch origin main
            if git merge-base --is-ancestor "$source" FETCH_HEAD; then
                return 0
            fi
            published=$(git tag --points-at "$source" --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[1-9][0-9]*$' || true)
            for tag in $published; do
                if gh release view "$tag" --json isPrerelease,isDraft --jq '.isPrerelease and (.isDraft | not)' | grep -qx true; then
                    return 0
                fi
            done
            # Git tags are durable allocations; include existing registry versions from before this workflow.
            available=$(gh api --paginate "/users/${repository%/*}/packages/container/${repository#*/}/versions" --jq '.[].metadata.container.tags[]')
            if [[ -n $available ]]; then
                mapfile -t registry_versions <<<"$available"
            fi
            version=$(sh scripts/bump-version.sh prerelease VERSION "${registry_versions[@]}")
            candidates=("$version")
            ;;
        *) echo "Unknown publication channel: $channel" >&2; return 1 ;;
    esac

    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
    for tag in "${candidates[@]}"; do
        for candidate in "$tag" "tpm-$tag"; do
            if lookup_digest "$image:$candidate" >/dev/null; then
                continue
            else
                status=$?
                [[ $status -eq 1 ]] || return "$status"
            fi
            if [[ $candidate == "$tag" ]]; then
                tags+=("$image:$candidate")
            else
                tpm_tags+=("$image:$candidate")
            fi
        done
    done
    {
        printf 'version=%s\nsource=%s\n' "$version" "$source"
        printf 'tags<<EOF\n'
        if (("${#tags[@]}")); then printf '%s\n' "${tags[@]}"; fi
        printf 'EOF\ntpm_tags<<EOF\n'
        if (("${#tpm_tags[@]}")); then printf '%s\n' "${tpm_tags[@]}"; fi
        printf 'EOF\nlabels<<EOF\n'
        printf 'org.opencontainers.image.source=https://github.com/%s\n' "$repository"
        printf 'org.opencontainers.image.revision=%s\n' "$source"
        printf 'org.opencontainers.image.version=%s\n' "$version"
        printf 'org.opencontainers.image.licenses=GPL-3.0-only\nEOF\n'
    } >> "$GITHUB_OUTPUT"
}

pull_public() {
    local ref=$1 config attempt
    config=$(mktemp -d)
    for attempt in {1..6}; do
        if docker --config "$config" pull --platform linux/amd64 "$ref"; then
            rmdir "$config"
            return
        fi
        if ((attempt < 6)); then
            sleep 10
        fi
    done
    rm -rf "$config"
    return 1
}

verify_image() {
    local ref=$1 source=$2 version=$3
    pull_public "$ref"
    docker image inspect "$ref" | jq -e \
        --arg source "$source" --arg version "$version" --arg repo "https://github.com/$repository" \
        '.[0] | .Os == "linux" and .Architecture == "amd64" and
         .Config.Labels["org.opencontainers.image.source"] == $repo and
         .Config.Labels["org.opencontainers.image.revision"] == $source and
         .Config.Labels["org.opencontainers.image.version"] == $version and
         .Config.Labels["org.opencontainers.image.licenses"] == "GPL-3.0-only"' >/dev/null
}

wait_digest() {
    local ref=$1 expected=${2:-} actual attempt status
    for attempt in {1..6}; do
        if actual=$(lookup_digest "$ref"); then
            if [[ -z $expected || $actual == "$expected" ]]; then
                printf '%s\n' "$actual"
                return
            fi
        else
            status=$?
            [[ $status -eq 1 ]] || return "$status"
        fi
        if ((attempt < 6)); then
            sleep 10
        fi
    done
    printf 'Expected manifest did not appear for %s\n' "$ref" >&2
    return 1
}

promote() {
    local alias=$1 digest=$2 current status
    if current=$(lookup_digest "$image:$alias"); then
        if [[ $current == "$digest" ]]; then
            return 0
        fi
        if [[ $alias == "$RELEASE_VERSION" || $alias == "tpm-$RELEASE_VERSION" ]]; then
            printf 'The version tag %s already points to a different digest.\n' "$alias" >&2
            return 1
        fi
    else
        status=$?
        [[ $status -eq 1 ]] || return "$status"
    fi
    docker buildx imagetools create --prefer-index=false -t "$image:$alias" "$image@$digest"
    wait_digest "$image:$alias" "$digest" >/dev/null
}

write_dev_draft() {
    local source=$1 notes=$2 releases="repos/$repository/releases" title='Development build'
    local drafts id draft response attempt
    # GitHub can replace a draft's requested tag name with an untagged-* placeholder.
    drafts=$(gh api --paginate "$releases" | jq -r --arg title "$title" '
        .[] | select(.tag_name == "dev" or
            (.draft and .name == $title and ((.tag_name // "") | startswith("untagged-"))))
        | [.id, .draft] | @tsv')
    if [[ -z $drafts ]]; then
        response=$(gh api --method POST "$releases" -f tag_name=dev -F draft=true \
            -f make_latest=false -f target_commitish="$source" -f name="$title" -f body="$notes")
    else
        IFS=$'\t' read -r id draft <<<"$drafts"
        [[ $draft == true && $drafts != *$'\n'* ]]
        response=$(gh api --method PATCH "$releases/$id" \
            -f target_commitish="$source" -f name="$title" -f body="$notes")
    fi
    id=$(jq -er '.id | numbers' <<<"$response")
    for attempt in {1..5}; do
        if gh api "$releases/$id" | jq -e --arg source "$source" --arg notes "$notes" --arg title "$title" \
            '.name == $title and .draft and (.prerelease | not) and
             .target_commitish == $source and .body == $notes' >/dev/null; then
            return
        fi
        if ((attempt < 5)); then
            sleep 2
        fi
    done
    printf 'Dev draft %s does not match commit %s.\n' "$id" "$source" >&2
    return 1
}

finish() {
    local source=${SOURCE_SHA:?} version=${RELEASE_VERSION:?}
    local tag="v$version" image_tag digest tpm_digest ref tpm_ref notes actual base remote_tag
    case $channel in
        dev|stable) image_tag="sha-$source" ;;
        deps) image_tag="$version" ;;
    esac
    digest=$(wait_digest "$image:$image_tag")
    tpm_digest=$(wait_digest "$image:tpm-$image_tag")
    ref="$image@$digest"
    tpm_ref="$image@$tpm_digest"
    verify_image "$ref" "$source" "$version"
    verify_image "$tpm_ref" "$source" "$version"
    sh scripts/smoke-container.sh "$ref" linux/amd64
    sh scripts/smoke-tpm.sh "$tpm_ref" "$ref"
    notes=$(printf 'Container image for linux/amd64: %s\n\nTPM service image for linux/amd64: %s\n\nSource: %s\nVersion: %s' "$ref" "$tpm_ref" "$source" "$version")

    case $channel in
        dev)
            actual=$(branch_head dev)
            [[ $actual == "$source" ]] || return 0
            promote tpm-dev "$tpm_digest"
            promote dev "$digest"
            write_dev_draft "$source" "$notes"
            ;;
        stable)
            promote "tpm-$version" "$tpm_digest"
            promote "$version" "$digest"
            base=$(git rev-parse "$source^")
            actual=$(branch_head main)
            if [[ $actual == "$base" ]]; then
                git push --atomic origin "$source:refs/heads/main" "$source:refs/tags/$tag"
            else
                [[ $actual == "$source" ]]
            fi
            remote_tag=$(git ls-remote --exit-code --tags origin "refs/tags/$tag" | cut -f 1)
            [[ $remote_tag == "$source" ]]
            if ! gh release view "$tag" -R "$repository" >/dev/null 2>&1; then
                gh release create "$tag" --verify-tag --title "$tag" --latest --notes "$notes" -R "$repository"
            fi
            [[ $(gh api "repos/$repository/releases/latest" --jq '.tag_name') == "$tag" ]]
            [[ $(branch_head main) == "$source" ]]
            promote tpm-latest "$tpm_digest"
            promote latest "$digest"
            ;;
        deps)
            actual=$(branch_head deps)
            [[ $actual == "$source" ]] || return 0
            remote_tag=$(git ls-remote --exit-code --tags origin "refs/tags/$tag" | cut -f 1)
            [[ $remote_tag == "$source" ]]
            gh release create "$tag" --verify-tag --title "$tag" --prerelease --latest=false --notes "$notes" -R "$repository"
            gh release view "$tag" -R "$repository" --json isPrerelease,isDraft \
                --jq '.isPrerelease and (.isDraft | not)' | grep -qx true
            ;;
    esac
}

case ${1:?} in
    prepare) prepare ;;
    finish) finish ;;
    *) exit 2 ;;
esac
