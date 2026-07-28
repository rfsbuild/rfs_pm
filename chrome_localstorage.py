#!/usr/bin/env python3
"""Read a localStorage key out of Chrome's on-disk LevelDB — while Chrome runs.

WHY THIS IS NOT A REGEX
    The first version of this scan searched the raw bytes for the key and then
    brace-matched forward to pull the JSON. That worked on 2026-07-28 only
    because the value still sat in the uncompressed write-ahead log. Hours
    later Chrome compacted it into an `.ldb` SST, where data blocks are
    **Snappy-compressed** — the same scan then produced a corrupt half-parse
    (literal text interleaved with back-reference bytes) and, worse, sometimes
    parses *almost* correctly. Reading compressed data as if it were plain is a
    silent-corruption bug, not a miss.

    So: parse the SST properly (footer -> index block -> data blocks),
    decompress each block, and search the decompressed bytes. `.log` files are
    uncompressed and are searched directly.

Chrome does not need to be closed; files are opened read-only and never
written. Requires `cramjam` for Snappy.
"""
import glob
import json
import os
import struct

import cramjam

SST_MAGIC = 0xdb4775248b80fb57
DEFAULT_DIR = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome/Default/Local Storage/leveldb"
)


def _varint(buf, pos):
    result = shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            break
    raise ValueError("bad varint")


def _read_block(blob, offset, size):
    """Return the decompressed contents of one block (data or index).

    Layout: [size bytes payload][1 byte compression type][4 byte crc].
    type 0 = none, 1 = snappy, 2 = zlib, 4 = zstd.
    """
    payload = blob[offset:offset + size]
    ctype = blob[offset + size]
    if ctype == 0:
        return bytes(payload)
    if ctype == 1:
        return bytes(cramjam.snappy.decompress_raw(payload))
    if ctype == 2:
        import zlib
        return zlib.decompress(payload)
    if ctype == 4:
        return bytes(cramjam.zstd.decompress(payload))
    raise ValueError("unknown block compression %d" % ctype)


def _block_handles(index_block):
    """Every entry value in the index block is a BlockHandle(offset, size)."""
    if len(index_block) < 4:
        return []
    num_restarts = struct.unpack("<I", index_block[-4:])[0]
    end = len(index_block) - 4 - num_restarts * 4
    handles, pos = [], 0
    while pos < end:
        try:
            _shared, pos = _varint(index_block, pos)
            non_shared, pos = _varint(index_block, pos)
            vlen, pos = _varint(index_block, pos)
        except ValueError:
            break
        pos += non_shared
        val = index_block[pos:pos + vlen]
        pos += vlen
        try:
            off, p2 = _varint(val, 0)
            size, _ = _varint(val, p2)
            handles.append((off, size))
        except ValueError:
            continue
    return handles


def _sst_blocks(path):
    """Yield every decompressed data block of an SST file."""
    with open(path, "rb") as f:
        blob = f.read()
    if len(blob) < 48:
        return
    footer = blob[-48:]
    if struct.unpack("<Q", footer[-8:])[0] != SST_MAGIC:
        return
    pos = 0
    _mi_off, pos = _varint(footer, pos)   # metaindex handle (unused)
    _mi_size, pos = _varint(footer, pos)
    idx_off, pos = _varint(footer, pos)
    idx_size, pos = _varint(footer, pos)
    try:
        index_block = _read_block(blob, idx_off, idx_size)
    except Exception:
        return
    for off, size in _block_handles(index_block):
        try:
            yield _read_block(blob, off, size)
        except Exception:
            continue


def _extract_json_after(buf, key):
    """Find `key` in a *decompressed* buffer and return the JSON object after it.

    Brace-matching is safe here because the bytes are real, not compressed.
    Chrome prefixes the value with 0x00 (UTF-16) or 0x01 (8-bit); only the
    8-bit form is handled, which is what a JSON.stringify payload produces.
    """
    out = []
    start = 0
    while True:
        i = buf.find(key, start)
        if i < 0:
            return out
        start = i + 1
        j = buf.find(b"{", i)
        if j < 0 or j - i > 200:
            continue
        depth, end, in_str, esc = 0, None, False, False
        for k in range(j, len(buf)):
            c = buf[k:k + 1]
            if in_str:
                if esc:
                    esc = False
                elif c == b"\\":
                    esc = True
                elif c == b'"':
                    in_str = False
                continue
            if c == b'"':
                in_str = True
            elif c == b"{":
                depth += 1
            elif c == b"}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            continue
        try:
            out.append(json.loads(buf[j:end].decode("utf-8")))
        except Exception:
            continue


def _activity(state):
    """How much user work a candidate contains.

    The recency discriminator. The same key exists in several files at once
    (write-ahead log + SSTs from different compaction generations), and *file
    mtime lies*: compaction rewrites an OLD value into a NEW file, so
    newest-mtime-wins can hand back a stale generation. Counting entries
    doesn't help either — every generation has the same 32 items.

    What does discriminate: within a working day she only ever ADDS work
    (ticks, defers, assignments, notes). The generation with the most set
    fields is therefore the latest. Caught 2026-07-28 when a re-read returned
    15 done / 8 notes for a board that genuinely had 16 / 9.
    """
    n = 0
    for v in state.values():
        if not isinstance(v, dict):
            continue
        if v.get("done"):
            n += 1
        if v.get("defer"):
            n += 1
        if v.get("assignee") and v.get("assignee") != "hadassa":
            n += 1
        if v.get("note"):
            n += 1
        n += int(v.get("deferDays") or 0)
    return n


def _file_generation(path):
    """LevelDB numbers its files monotonically (030596.ldb > 030430.ldb), which
    is a far better recency signal than mtime."""
    base = os.path.basename(path)
    stem = base.split(".")[0]
    return int(stem) if stem.isdigit() else -1


def read_key(key, leveldb_dir=DEFAULT_DIR, return_all=False):
    """Return the most-recent parse of `key`'s JSON value, or None.

    Ranks every candidate found across every file by (user-activity, file
    generation) — see _activity() for why neither size nor mtime works.
    """
    kb = key.encode() if isinstance(key, str) else key
    found = []
    for path in glob.glob(os.path.join(leveldb_dir, "*")):
        name = os.path.basename(path)
        candidates = []
        try:
            if name.endswith(".ldb") or name.endswith(".sst"):
                for block in _sst_blocks(path):
                    candidates.extend(_extract_json_after(block, kb))
            else:
                with open(path, "rb") as f:
                    candidates.extend(_extract_json_after(f.read(), kb))
        except Exception:
            continue
        gen = _file_generation(path)
        for obj in candidates:
            if isinstance(obj, dict):
                good = {k: v for k, v in obj.items() if isinstance(v, dict)}
                if good:
                    found.append((_activity(good), gen, len(good), good))
    if not found:
        return [] if return_all else None
    found.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    if return_all:
        return [{"activity": a, "generation": g, "items": n} for a, g, n, _ in found]
    return found[0][3]


if __name__ == "__main__":
    import sys
    k = sys.argv[1] if len(sys.argv) > 1 else "pmbrief_state_2026-07-28"
    val = read_key(k)
    print(json.dumps(val, indent=1, ensure_ascii=False) if val else "NOT FOUND")
