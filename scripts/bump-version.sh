#!/bin/sh
# Print the next version. stable increments Y and resets Z. prerelease increments Z
# above every tag already allocated in that X.Y series.
set -eu
mode="${1:?stable or prerelease}"
file="${2:?VERSION file}"
shift 2
current=$(tr -d '[:space:]' < "$file")
case "$current" in
  *.*.*) ;;
  *) echo "bad version: $current" >&2; exit 1 ;;
esac
X=${current%%.*}
rest=${current#*.}
Y=${rest%%.*}
Z=${rest#*.}
case "$X$Y$Z" in
  *[!0-9]*) echo "bad version: $current" >&2; exit 1 ;;
esac
case "$mode" in
  stable)
    Y=$((Y + 1))
    Z=0
    ;;
  prerelease)
    high=$Z
    if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
      set -- "$@" $(git tag -l "v${X}.${Y}.*")
    fi
    for allocated do
      tag=${allocated#v}
      case "$tag" in
        "$X.$Y."*) tz=${tag#"$X.$Y."} ;;
        *) continue ;;
      esac
      case "$tz" in
        ''|0[0-9]*|*[!0-9]*) continue ;;
      esac
      if [ "$tz" -gt "$high" ]; then
        high=$tz
      fi
    done
    Z=$((high + 1))
    ;;
  *)
    echo "unknown mode: $mode" >&2
    exit 1
    ;;
esac
printf '%s.%s.%s\n' "$X" "$Y" "$Z"
