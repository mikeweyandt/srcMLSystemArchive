#!/usr/bin/env bash
# Run srcml over a checked-out source tree and produce a compressed archive.
#
# Adapted from srcMLLargeSystems/scripts/generate_baseline.sh, with three
# changes that the 500-system scale forces:
#
#   1. srcml is piped straight into zstd. The kernel is ~1.4 GB of C and its
#      srcML XML runs to many GB; materializing that intermediate on a runner
#      with ~20 GB free is the difference between working and not.
#   2. Uncompressed size and unit counts come from one decompression pass
#      (scripts/archive_stats.py), which doubles as an integrity check.
#   3. A hard guard on GitHub's 2 GiB release-asset limit, checked before the
#      upload rather than discovered by a failed upload an hour later.
#
# Required environment:
#   INPUT_DIR      directory to parse
#   OUTPUT_ZST     path for the compressed archive
#   STATS_JSON     path for the stats sidecar
#   LOG_FILE       path to tee progress into
# Optional:
#   SRCML_BIN      srcml binary (default: srcml)
#   SRCML_FLAGS    space-separated flags
#   ZSTD_LEVEL     compression level (default: 19)
#   MAX_ASSET_BYTES  release-asset ceiling (default: 2 GiB)

set -euo pipefail

: "${SRCML_BIN:=srcml}"
: "${SRCML_FLAGS:=-r -j 4 --src-encoding=UTF-8}"
: "${ZSTD_LEVEL:=19}"
: "${MAX_ASSET_BYTES:=2147483648}"
: "${INPUT_DIR:?INPUT_DIR required}"
: "${OUTPUT_ZST:?OUTPUT_ZST required}"
: "${STATS_JSON:?STATS_JSON required}"
: "${LOG_FILE:=generate.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { echo "$@" | tee -a "$LOG_FILE"; }

: > "$LOG_FILE"
log "srcml version: $("$SRCML_BIN" --version 2>&1 | head -1)"
log "input dir:     $INPUT_DIR"
log "flags:         $SRCML_FLAGS"
log "output:        $OUTPUT_ZST"
log "zstd level:    $ZSTD_LEVEL"
log "start:         $(date --iso-8601=seconds)"
log "disk before:   $(df -h --output=avail . | tail -1 | tr -d ' ') available"

# srcml writes the archive to stdout when no -o is given. Piping into zstd keeps
# the multi-GB XML off disk entirely. pipefail (set above) makes a srcml crash
# fail the pipeline rather than silently yielding a truncated archive.
#
# srcml's exit status is captured rather than asserted: across a 500-repo corpus
# some inputs will make it complain, and an archive that still decompresses and
# parses is worth keeping. The integrity check below is the real gate.
# srcml records each filename exactly as it received it, and has no option to
# rewrite or relativize them. That makes how it is invoked the only lever:
#
#   srcml -r project-src   ->  project-src/v2rayN/Program.cs   (scratch dir leaks)
#   srcml -r .             ->  /home/runner/work/.../Program.cs (canonicalized!)
#   srcml -r v2rayN ...    ->  v2rayN/Program.cs                (what we want)
#
# A named relative path is preserved verbatim, but `.` gets canonicalized to an
# absolute path — so the entries are named explicitly from inside the checkout.
# Only srcml's directory changes; the subshell leaves zstd and the log in the
# workspace.
set +e
(
    cd "$INPUT_DIR" || exit 1
    shopt -s nullglob
    entries=(*)
    if [ ${#entries[@]} -eq 0 ]; then
        echo "ERROR: no top-level entries in $INPUT_DIR" >&2
        exit 1
    fi
    # shellcheck disable=SC2086
    "$SRCML_BIN" $SRCML_FLAGS "${entries[@]}"
) \
    2> >(tee -a "$LOG_FILE" >&2) \
    | zstd "-${ZSTD_LEVEL}" --long=27 -T0 --force -o "$OUTPUT_ZST"
pipe_status=("${PIPESTATUS[@]}")
set -e
srcml_exit=${pipe_status[0]}
zstd_exit=${pipe_status[1]}

# A compressor failure is never tolerable; a parser complaint might be.
if [ "$zstd_exit" -ne 0 ]; then
    log "ERROR: zstd exited $zstd_exit"
    exit 1
fi
if [ ! -s "$OUTPUT_ZST" ]; then
    log "ERROR: srcml produced no output (exit $srcml_exit)"
    exit 1
fi
if [ "$srcml_exit" -ne 0 ]; then
    log "WARNING: srcml exited $srcml_exit; validating the archive anyway"
    echo "::warning::srcml exited $srcml_exit for $INPUT_DIR"
fi

zst_bytes=$(stat -c%s "$OUTPUT_ZST")
sha=$(sha256sum "$OUTPUT_ZST" | awk '{print $1}')
log "compressed:    $zst_bytes bytes"
log "sha256:        $sha"

# Well-formedness gate. This is what catches a srcml crash mid-stream: the
# archive is left truncated, so it still opens with an XML declaration and still
# contains units, but its root element never closes. Only a real parse detects
# that. --stream keeps libxml2 in SAX mode, so memory stays flat no matter how
# many GB the archive runs to.
if [ "${SKIP_XML_VALIDATION:-0}" != "1" ]; then
    log "validating XML well-formedness..."
    if ! zstd -dc "$OUTPUT_ZST" | xmllint --stream --noout - 2> >(tee -a "$LOG_FILE" >&2); then
        log "ERROR: archive is not well-formed XML (srcml exited $srcml_exit — likely truncated)"
        exit 1
    fi
fi

# Second streaming pass for uncompressed size and the per-language unit
# breakdown. Kept separate from validation because zstd decompresses at roughly
# a GB/s, making two clean passes cheaper than one entangled one.
log "collecting stats..."
zstd -dc "$OUTPUT_ZST" | python3 "$SCRIPT_DIR/archive_stats.py" --out "$STATS_JSON" \
    2> >(tee -a "$LOG_FILE" >&2)

raw_bytes=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["bytes_raw"])' "$STATS_JSON")
units=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["units"])' "$STATS_JSON")

if [ "$units" -eq 0 ]; then
    log "ERROR: archive contains zero srcML units — nothing was parsed"
    exit 1
fi

if [ "$zst_bytes" -gt "$MAX_ASSET_BYTES" ]; then
    log "ERROR: $OUTPUT_ZST is $zst_bytes bytes, over the $MAX_ASSET_BYTES release-asset limit"
    exit 1
fi

ratio=$(python3 -c "print(f'{$raw_bytes / max($zst_bytes,1):.1f}')")
log "uncompressed:  $raw_bytes bytes (${ratio}x)"
log "units:         $units"
log "disk after:    $(df -h --output=avail . | tail -1 | tr -d ' ') available"
log "end:           $(date --iso-8601=seconds)"

{
    echo "raw_bytes=$raw_bytes"
    echo "zst_bytes=$zst_bytes"
    echo "sha256=$sha"
    echo "units=$units"
    echo "srcml_exit=$srcml_exit"
} >> "${GITHUB_OUTPUT:-/dev/null}"
