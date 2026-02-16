#!/usr/bin/env python3
"""
Memory usage visualization tool (memwiz).

Renders SoC memory schema to HTML. When a linker map file is provided (later),
overlays linker memories and segments on the schema.

- Memory blocks: start address at top, address grows downward; fixed width;
  minimum height for visibility.
- All addresses appear in the left (IRAM) or right (DRAM) columns only.
- Central block shows only the memory name and size label.

Terminology (do not mix):
- Linker segment: A memory region from the map file "Memory Configuration" table
  (Name, Attr, Origin, Length). Segment length and origin come only from this table.
  Segments are also described in platform/soc YAML (name, begin/end symbols) and
  resolved to [start, end) using map symbols.
- Memory bus: A row/region from the SoC YAML (memory.buses). Defines layout: which
  row, left/right column, address range of the block. Segments are drawn *within*
  buses when their [start, end) overlaps a bus; the drawn "strip" is the segment
  clipped to that bus. Bus size is for layout only, not for segment length.

Column layout (use these numbers as reference):
  1. Left markers   (address markers; any additional symbols to display go here)
  2. Left bus       (used segment begin/end only for left-side segments)
  3. Center         (memory block: strips + label)
  4. Right bus      (used segment begin/end only for right-side segments)
  5. Right markers  (address markers; any additional symbols to display go here)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("memwiz requires PyYAML. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Size and address parsing
# ---------------------------------------------------------------------------

SIZE_RE = re.compile(r"^\s*(\d+)\s*([kmg]?b?)\s*$", re.IGNORECASE)
K = 1024
M = 1024 * K
G = 1024 * M


def parse_size(s: str) -> int:
    """Parse human-readable size (e.g. '32k', '416kb') to bytes."""
    if isinstance(s, int):
        return s
    s = str(s).strip()
    m = SIZE_RE.match(s)
    if not m:
        raise ValueError(f"Invalid size: {s!r}")
    num = int(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit in ("", "b"):
        return num
    if unit in ("k", "kb"):
        return num * K
    if unit in ("m", "mb"):
        return num * M
    if unit in ("g", "gb"):
        return num * G
    raise ValueError(f"Unknown size unit: {unit!r}")


def parse_addr(addr) -> int:
    """Parse hex address to int."""
    if isinstance(addr, int):
        return addr
    s = str(addr).strip()
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


# ---------------------------------------------------------------------------
# SoC schema loading
# ---------------------------------------------------------------------------

# Match a line that is exactly "bus:" (with optional leading/optional trailing whitespace)
_BUS_KEY_RE = re.compile(r"^(\s*)bus\s*:\s*$", re.IGNORECASE)
_NAME_RE = re.compile(r"^\s*name\s*:\s*(.+)$", re.IGNORECASE)
_SIZE_RE = re.compile(r"^\s*size\s*:\s*(.+)$", re.IGNORECASE)


def _parse_memory_block(block: str) -> dict | None:
    """Parse one 'memory' block (string). Returns dict with 'name', optional 'size', and 'buses' list."""
    lines = block.splitlines()
    if not lines:
        return None
    name = None
    size = None  # memory-level size (bytes, parsed)
    buses: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        name_m = _NAME_RE.match(line)
        if name_m:
            name = name_m.group(1).strip().strip("'\"")
            i += 1
            continue
        size_m = _SIZE_RE.match(line)
        if size_m:
            try:
                size = parse_size(size_m.group(1).strip().strip("'\""))
            except (ValueError, TypeError):
                pass
            i += 1
            continue
        bus_m = _BUS_KEY_RE.match(line)
        if bus_m:
            indent = len(bus_m.group(1))
            # Collect following lines that are indented more than this "bus:" line
            bus_lines: list[str] = []
            i += 1
            while i < len(lines):
                next_line = lines[i]
                if not next_line.strip():
                    i += 1
                    continue
                # Count leading spaces (or tabs) for indent
                line_indent = len(next_line) - len(next_line.lstrip())
                if line_indent <= indent:
                    break
                bus_lines.append(next_line)
                i += 1
            if bus_lines:
                bus_str = "\n".join(bus_lines)
                try:
                    bus_data = yaml.safe_load(bus_str)
                except Exception:
                    bus_data = None
                if bus_data is not None:
                    if isinstance(bus_data, list):
                        buses.extend(bus_data)
                    else:
                        buses.append(bus_data)
            continue
        i += 1
    if name is None and not buses:
        return None
    out: dict = {"name": name or "?", "buses": buses}
    if size is not None:
        out["size"] = size
    return out


def _parse_separator_block(block: str) -> dict | None:
    """Parse a 'separator:' block. Returns {'name': name} if name provided, else {}; None if not a separator block."""
    if not re.match(r"^\s*separator\s*:\s*", block.strip(), re.IGNORECASE):
        return None
    try:
        data = yaml.safe_load(block.strip())
        if data and "separator" in data:
            s = data["separator"]
            if isinstance(s, dict) and "name" in s:
                return {"name": str(s["name"]).strip()}
            return {}
    except Exception:
        pass
    return {}


def load_soc_schema(path: Path) -> list[dict]:
    """Load SoC memory schema from YAML. Supports multiple top-level 'memory:' entries and
    multiple 'bus:' entries per memory. Returns list of memory dicts with normalized 'buses'.
    A top-level 'separator:' block (with optional 'name') applies to the next memory block."""
    content = path.read_text(encoding="utf-8")
    # Split by "memory:" at line boundary so we get one block per memory
    blocks = re.split(r"(?m)^memory\s*:\s*$", content, flags=re.IGNORECASE)
    memories: list[dict] = []
    pending_separator: dict | None = None
    for raw_block in blocks:
        block = raw_block.strip()
        if not block or block.startswith("#"):
            continue
        # A block may contain "separator:" after the memory; separator applies to the *next* memory
        sep_match = re.search(r"(?m)^separator\s*:\s*", block, re.IGNORECASE)
        memory_part = block
        separator_in_this_block = False
        if sep_match:
            memory_part = block[: sep_match.start()].strip()
            separator_part = block[sep_match.start() :].strip()
            sep = _parse_separator_block(separator_part)
            if sep is not None:
                pending_separator = sep if sep else None
                separator_in_this_block = True
        else:
            sep = _parse_separator_block(block)
            if sep is not None:
                pending_separator = sep if sep else None
                continue
        if not memory_part:
            continue
        # When separator was in this block, do not attach it to this memory (it applies to next)
        apply_sep = pending_separator is not None and not separator_in_this_block
        # Handle list format: "  - name: SRAM0\n    bus: ..." (single list under this memory)
        if re.match(r"^\s*-\s*", memory_part):
            try:
                wrapped = "mem:\n" + memory_part
                data = yaml.safe_load(wrapped)
                if data and "mem" in data:
                    items = data["mem"] if isinstance(data["mem"], list) else [data["mem"]]
                    for item in items:
                        m = dict(item)
                        if "bus" in m:
                            b = m.pop("bus")
                            m["buses"] = b if isinstance(b, list) else [b]
                        else:
                            m["buses"] = m.pop("buses", [])
                        if apply_sep:
                            m["separator_before"] = pending_separator
                            pending_separator = None
                        memories.append(m)
            except Exception:
                parsed = _parse_memory_block(memory_part)
                if parsed:
                    if apply_sep:
                        parsed["separator_before"] = pending_separator
                        pending_separator = None
                    memories.append(parsed)
            continue
        parsed = _parse_memory_block(memory_part)
        if parsed:
            if apply_sep:
                parsed["separator_before"] = pending_separator
                pending_separator = None
            memories.append(parsed)
    return memories


def normalize_bus(b: dict, _base_dir: Path, memory_size: int | None = None) -> dict:
    """Normalize bus: start/size as integers, end = start + size, default display side.
    If bus has no 'size', use memory_size (memory-level size from YAML).
    reversed: if True, start address is at bottom of block and address increases upward."""
    out = dict(b)
    out["start"] = parse_addr(out["start"])
    if "size" in out:
        out["size"] = parse_size(out["size"])
    elif memory_size is not None:
        out["size"] = memory_size
    else:
        raise ValueError("Bus has no 'size' and memory has no 'size'")
    out["end"] = out["start"] + out["size"]
    out.setdefault("name", "")
    out.setdefault("display", "left")
    out["reversed"] = bool(out.get("reversed", False))
    return out


# ---------------------------------------------------------------------------
# Map file and platform segments
# ---------------------------------------------------------------------------

# GNU ld map: " 0x40370000                _stext = ." or "0x40370000  _init_start"
_MAP_SYMBOL_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s+(\S+)(?:\s*=.*)?$")


def parse_map_symbols(path: Path) -> dict[str, int]:
    """Parse a GNU ld map file; return symbol name -> address."""
    symbols: dict[str, int] = {}
    content = path.read_text(errors="replace")
    for line in content.splitlines():
        m = _MAP_SYMBOL_RE.match(line)
        if m:
            addr_str, name = m.groups()
            if name and not name.startswith("."):
                symbols[name] = int(addr_str, 16)
    return symbols


# GNU ld map: output section line like " .section_name" then next line " 0xADDR  SIZE" or "0xADDR  SIZE load address ..."
_MAP_SECTION_HEADER_RE = re.compile(r"^\s*\.([A-Za-z0-9_.]+)\s*$")
_MAP_SECTION_ADDR_SIZE_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s+(\d+|0x[0-9a-fA-F]+)")


def parse_map_sections(path: Path) -> list[dict]:
    """Parse a GNU ld map file for output sections; return list of {name, start, size, block_name}.
    name is the section name; block_name is the declared section block (output section) from the map."""
    sections: list[dict] = []
    content = path.read_text(errors="replace")
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = _MAP_SECTION_HEADER_RE.match(lines[i])
        if m:
            sec_name = "." + m.group(1)
            block_name = sec_name  # output section in map = section block (e.g. .iram0.vectors)
            i += 1
            while i < len(lines):
                addr_m = _MAP_SECTION_ADDR_SIZE_RE.match(lines[i])
                if addr_m:
                    start = int(addr_m.group(1), 16)
                    size_str = addr_m.group(2)
                    size = int(size_str, 16) if size_str.startswith("0x") else int(size_str, 10)
                    sections.append({
                        "name": sec_name,
                        "start": start,
                        "size": size,
                        "block_name": block_name,
                    })
                    i += 1
                    break
                if lines[i].strip() and not lines[i].strip().startswith("*"):
                    break
                i += 1
            continue
        i += 1
    return sections


# Memory Configuration in map file: section "Memory Configuration" then table.
# Table has segment name (text), optional attr (text), and Origin/Length (hex).
# Column order varies (e.g. Name, Origin, Length, Attr or Name, Attr, Origin, Length).
_HEX_TOKEN_RE = re.compile(r"^(0x[0-9a-fA-F]+|[0-9]+)$", re.IGNORECASE)


def _parse_memory_config_line(line: str) -> tuple[str, int, int] | None:
    """Parse one Memory Configuration data line. Returns (name, origin, length) or None.
    Name is first token; origin and length are the first two hex/decimal tokens after it."""
    line = line.strip()
    if not line:
        return None
    tokens = line.split()
    if len(tokens) < 3:
        return None
    name = tokens[0]
    hex_values: list[int] = []
    for t in tokens[1:]:
        if _HEX_TOKEN_RE.match(t):
            val = int(t, 16) if t.startswith("0x") else int(t, 10)
            hex_values.append(val)
            if len(hex_values) == 2:
                break
    if len(hex_values) != 2:
        return None
    return (name, hex_values[0], hex_values[1])


def parse_map_memory_config(path: Path) -> dict[str, dict]:
    """Parse the Memory Configuration table from a GNU ld map file.
    Segment name is first column; origin and length are the first two hex/numeric columns after it.
    Returns dict: segment_name -> {origin: int, length: int} (length in bytes)."""
    result: dict[str, dict] = {}
    content = path.read_text(errors="replace")
    lines = content.splitlines()
    in_section = False
    for line in lines:
        if "Memory Configuration" in line:
            in_section = True
            continue
        if in_section:
            if line.strip() and "Linker script" in line:
                break
            # Skip header line (typical: "Name  Origin  Length" or "NAME  ORIGIN  LENGTH  ATTR")
            stripped = line.strip()
            tokens_upper = [t.upper() for t in stripped.split()]
            if stripped.upper().startswith("NAME") or "ORIGIN" in tokens_upper or "LENGTH" in tokens_upper:
                continue
            parsed = _parse_memory_config_line(line)
            if parsed:
                name, origin, length = parsed
                result[name] = {"origin": origin, "length": length}
    return result


def _memory_config_for_segment(memory_config: dict[str, dict], seg_name: str) -> dict | None:
    """Look up Memory Configuration row by segment name (exact, then case-insensitive)."""
    if not memory_config:
        return None
    if seg_name in memory_config:
        return memory_config[seg_name]
    seg_lower = seg_name.lower()
    for name, row in memory_config.items():
        if name.lower() == seg_lower:
            return row
    return None


def segment_input_sections(
    resolved_segments: list[dict], map_sections: list[dict]
) -> None:
    """Set input_sections on each segment from map file: section names that overlap [start, end).
    Each entry is 'section_name (section block: block_name)' using the declared section block from the map."""
    for seg in resolved_segments:
        start, end = seg["start"], seg["end"]
        entries: list[str] = []
        for s in map_sections:
            s_start = s["start"]
            s_end = s["start"] + s["size"]
            if s_end > start and s_start < end:
                name = s["name"]
                block = s.get("block_name", name)
                entries.append(f"{name} (section block: {block})")
        seg["input_sections"] = sorted(entries)


def _parse_segment_block(block: str) -> dict | None:
    """Parse one 'segment:' block. Returns dict with name, begin, end (and display inferred), or None."""
    block = block.strip()
    if not block:
        return None
    try:
        # Normalize to same indent so name/begin/end are siblings under a wrapper
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        indented = "\n  ".join(lines)
        wrapped = "seg:\n  " + indented
        data = yaml.safe_load(wrapped)
        if not data or "seg" not in data:
            return None
        seg = data["seg"] if isinstance(data["seg"], dict) else None
        if not seg:
            return None
        name = seg.get("name", "")
        begin = seg.get("begin")
        end = seg.get("end")
        if not name or begin is None or end is None:
            return None
        # Infer display: DRAM/DROM (data) -> right, IRAM/IROM (code) -> left
        name_lower = name.lower()
        display = seg.get(
            "display",
            "right" if ("dram" in name_lower or "drom" in name_lower) else "left",
        )
        return {"name": name, "begin": begin, "end": end, "display": display}
    except Exception:
        return None


def load_platform_segments(path: Path) -> list[dict]:
    """Load segment definitions from platform YAML (e.g. zephyr.yaml). Uses same notation as SoC: repeated
    top-level 'segment:' entries. Returns list of {name, begin, end, display}. input_sections come from the map file."""
    content = path.read_text(encoding="utf-8")
    blocks = re.split(r"(?m)^segment\s*:\s*$", content, flags=re.IGNORECASE)
    segments: list[dict] = []
    for raw_block in blocks:
        block = raw_block.strip()
        if not block or block.startswith("#"):
            continue
        parsed = _parse_segment_block(block)
        if parsed:
            segments.append(parsed)
    return segments


def resolve_segments(platform_segments: list[dict], symbols: dict[str, int]) -> list[dict]:
    """Resolve begin/end symbol names to addresses. Returns list of {name, start, end, display}."""
    resolved = []
    for seg in platform_segments:
        begin_sym = seg.get("begin")
        end_sym = seg.get("end")
        if not begin_sym or not end_sym:
            continue
        start = symbols.get(begin_sym)
        end = symbols.get(end_sym)
        if start is None or end is None:
            continue
        resolved.append({
            "name": seg.get("name", "?"),
            "start": start,
            "end": end,
            "display": seg.get("display", "left"),
            "begin_sym": seg.get("begin"),
            "end_sym": seg.get("end"),
        })
    return resolved


def _segment_strips_in_bus(segment: dict, bus: dict) -> tuple[float, float] | None:
    """If segment [start,end) overlaps bus [start,end), return (top_pct, height_pct) in 0..1, else None."""
    seg_start = segment["start"]
    seg_end = segment["end"]
    bus_start = bus["start"]
    bus_end = bus["end"]
    if seg_end <= bus_start or seg_start >= bus_end:
        return None
    # Clip segment to bus range
    clip_start = max(seg_start, bus_start)
    clip_end = min(seg_end, bus_end)
    bus_size = bus_end - bus_start
    top_pct = (clip_start - bus_start) / bus_size
    height_pct = (clip_end - clip_start) / bus_size
    return (top_pct, height_pct)


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

MIN_BLOCK_HEIGHT_PX = 64
BLOCK_WIDTH_PX = 260  # Wide enough for strips at edges + addresses shifted toward center
SEGMENT_STRIP_WIDTH_PX = 10
SEGMENT_LINE_HEIGHT_PX = 12  # segment address row height; center aligns with strip edge
SEGMENT_COLOR_ALLOCATED = "#c00"
LABEL_FONT = "12px system-ui, sans-serif"

# Size (bytes) -> byte/px for block height: height_px = size / byte_per_px (largest size <= block size)
SIZE_TO_BYTE_PER_PX = [
    (16 * 1024, 400),       # 16384
    (32 * 1024, 400),       # 32768
    (64 * 1024, 400),       # 65536
    (128 * 1024, 500),      # 131072
    (256 * 1024, 1000),     # 262144
    (512 * 1024, 1500),     # 524288
    (1024 * 1024, 3500),    # 1048576
    (2 * 1024 * 1024, 4000),   # 2097152
    (4 * 1024 * 1024, 4500),   # 4194304
    (8 * 1024 * 1024, 12000),  # 8388608
    (16 * 1024 * 1024, 18000), # 16777216
    (32 * 1024 * 1024, 30000), # 33554432
    (64 * 1024 * 1024, 50000), # 67108864
    (128 * 1024 * 1024, 80000),   # 134217728
    (256 * 1024 * 1024, 120000),  # 268435456
    (512 * 1024 * 1024, 200000),  # 536870912
    (1024 * 1024 * 1024, 350000), # 1073741824
]


def _byte_per_px_for_size(size: int) -> int:
    """Return byte/px for the given block size (bytes). Uses largest table size <= size, or first row if size smaller."""
    for s, bpp in reversed(SIZE_TO_BYTE_PER_PX):
        if size >= s:
            return bpp
    return SIZE_TO_BYTE_PER_PX[0][1]


# Single scale for strip heights so the same byte count looks the same height in every block (naked-eye comparable)
GLOBAL_BYTE_PER_PX = 400


def format_addr(addr: int) -> str:
    return f"0x{addr >> 16:04X}_{addr & 0xFFFF:04X}"


def _html_attr_escape(s: str) -> str:
    """Escape for HTML attribute value."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _c_id(s: str) -> str:
    """Memory name to C macro prefix (e.g. SRAM1 -> SRAM1, 'Some Region' -> SOME_REGION)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_").upper() or "MEM"


def format_size_kb(size_bytes: int) -> str:
    return f"{size_bytes // 1024} kB"


def _opposite_bus_addr(
    addr: int, side: str, row: dict
) -> tuple[str, str] | None:
    """Given an address on one bus (left/right), return (formatted_opposite_addr, opposite_label) or None.
    Uses iram_dram_offset (or IROM-DROM: same formula) for normal mapping; C-macro logic when reversed."""
    offset = row.get("iram_dram_offset")
    if offset is None:
        return None
    left_start = row["iram_start"]
    right_start = row["dram_start"]
    left_name = row.get("left_bus_name", "IRAM")
    right_name = row.get("right_bus_name", "DRAM")
    size = row["size"]
    rev = row.get("reversed", False)
    if side == "left":
        if rev:
            opp = size - (addr - left_start) + right_start
        else:
            opp = addr - offset
        return (format_addr(opp), right_name + "*")
    else:
        if rev:
            opp = size - (addr - right_start) + left_start
        else:
            opp = addr + offset
        return (format_addr(opp), left_name + "*")


def render_soc_html(
    memories: list[dict],
    soc_name: str,
    title: str | None = None,
    resolved_segments: list[dict] | None = None,
    memory_config: dict[str, dict] | None = None,
) -> str:
    """Generate HTML: central stack of physical memory blocks; addresses in left/right columns.
    If resolved_segments is provided, overlay segment strips (thick colored lines) inside the middle column:
    IRAM segments on left inner side, DRAM on right inner side; allocated = red."""
    title = title or f"{soc_name} memory model"
    base_dir = Path(".")
    resolved_segments = resolved_segments or []
    memory_config = memory_config or {}

    rows: list[dict] = []
    for m in memories:
        name = m.get("name", "?")
        mem_size = m.get("size")
        if isinstance(mem_size, str):
            mem_size = parse_size(mem_size)
        buses = [normalize_bus(b, base_dir, mem_size) for b in m.get("buses", [])]
        if not buses:
            continue
        size = mem_size if mem_size is not None else max(b["size"] for b in buses)
        left_buses = [b for b in buses if b.get("display") != "right"]
        right_buses = [b for b in buses if b.get("display") == "right"]
        row = {
            "name": name,
            "size": size,
            "left_buses": left_buses,
            "right_buses": right_buses,
            "left_segment_strips": [],
            "right_segment_strips": [],
        }
        if "separator_before" in m:
            row["separator_before"] = m["separator_before"]
        # Dual-bus offset and conversion (when both buses present). Supports IRAM/DRAM and IROM/DROM.
        if left_buses and right_buses:
            left_start = left_buses[0]["start"]
            right_start = right_buses[0]["start"]
            row["left_bus_name"] = left_buses[0].get("name", "IRAM")
            row["right_bus_name"] = right_buses[0].get("name", "DRAM")
            row["iram_dram_offset"] = left_start - right_start  # same formula for IROM-DROM: left_start - right_start
            row["iram_start"] = left_start
            row["dram_start"] = right_start
            row["reversed"] = bool(
                left_buses[0].get("reversed", False) or right_buses[0].get("reversed", False)
            )
        # Match linker segments to buses: one strip per segment, on its native side only, when range overlaps that bus.
        # Segment column shows only the start/end address of that strip (no mirror strips, no extra addresses).
        for seg in resolved_segments:
            display = seg.get("display", "left")
            seg_name = seg.get("name", "?")
            seg_start, seg_end = seg["start"], seg["end"]
            # --- Linker-segment properties (from map / resolved segment only; do not use bus here) ---
            start_addr = format_addr(seg_start)
            end_addr = format_addr(seg_end)
            start_sym = seg.get("begin_sym") or ""
            end_sym = seg.get("end_sym") or ""
            used_bytes = seg_end - seg_start
            mc = _memory_config_for_segment(memory_config, seg_name)
            if mc:
                segment_length = mc["length"]  # Length column from Memory Configuration (map) only
                available_end_num = mc["origin"] + mc["length"]
                available_end_addr = format_addr(available_end_num)
            else:
                segment_length = None
                available_end_addr = None
                available_end_num = None
            # --- Bus-only: strip geometry (top_pct, height_pct, used_in_this_block) ---
            def _apply_strip(which: str, strip: tuple[float, float], bus: dict) -> None:
                bus_start, bus_end = bus["start"], bus["end"]
                bus_size = bus_end - bus_start
                clip_start = max(seg_start, bus_start)
                clip_end = min(seg_end, bus_end)
                used_in_this_block = clip_end - clip_start
                top_pct = (clip_start - bus_start) / bus_size
                height_pct = used_in_this_block / bus_size
                contains_start = bus_start <= seg_start < bus_end
                contains_end = bus_start < seg_end <= bus_end
                # Available end (origin+length) position scaled to this block: 0..1 when inside row
                available_end_top_pct: float | None = None
                if available_end_num is not None and bus_size > 0 and bus_start <= available_end_num <= bus_end:
                    available_end_top_pct = (available_end_num - bus_start) / bus_size
                strip_data = {
                    "top_pct": top_pct, "height_pct": height_pct, "color": SEGMENT_COLOR_ALLOCATED,
                    "name": seg_name,
                    "start_addr": start_addr, "end_addr": end_addr,
                    "start_num": seg_start, "end_num": seg_end,
                    "start_sym": start_sym, "end_sym": end_sym,
                    "contains_start": contains_start, "contains_end": contains_end,
                    "input_sections": seg.get("input_sections", []),
                    "available_bytes": segment_length,
                    "available_end_addr": available_end_addr,
                    "available_end_top_pct": available_end_top_pct,
                    "used_bytes": used_bytes,
                    "used_in_this_block": used_in_this_block,
                }
                if which == "left":
                    row["left_segment_strips"].append(strip_data)
                else:
                    row["right_segment_strips"].append(strip_data)

            if display != "right":
                for bus in left_buses:
                    strip = _segment_strips_in_bus(seg, bus)
                    if strip:
                        _apply_strip("left", strip, bus)
                        break
            else:
                for bus in right_buses:
                    strip = _segment_strips_in_bus(seg, bus)
                    if strip:
                        _apply_strip("right", strip, bus)
                        break
        # Only on CACHE (IROM + DROM): place right (DROM) strips below left (IROM) so they don't overlap.
        # On SRAM1 (IRAM + DRAM) left and right are separate regions; do not reposition.
        if (
            left_buses and right_buses
            and row["left_segment_strips"] and row["right_segment_strips"]
            and row.get("left_bus_name") == "IROM" and row.get("right_bus_name") == "DROM"
        ):
            irom_end_max = max(
                s["top_pct"] + s["height_pct"] for s in row["left_segment_strips"]
            )
            total_drom_height = sum(s["height_pct"] for s in row["right_segment_strips"])
            irom_end_capped = min(irom_end_max, 1.0 - total_drom_height)
            if irom_end_capped < 1.0 and total_drom_height > 0:
                band_start = irom_end_capped
                for s in row["right_segment_strips"]:
                    s["top_pct"] = band_start
                    s["height_pct"] = min(s["height_pct"], 1.0 - band_start)
                    band_start += s["height_pct"]
        rows.append(row)

    if not rows:
        return "<!DOCTYPE html><html><body><p>No memories.</p></body></html>"

    # height = size / byte_per_px from conversion table; enforce minimum
    heights = [
        max(MIN_BLOCK_HEIGHT_PX, int(r["size"] / _byte_per_px_for_size(r["size"])))
        for r in rows
    ]

    def _addr_at_top_bottom(bus: dict) -> tuple[str, str]:
        """Return (addr_at_top, addr_at_bottom). For reversed bus, start is at bottom, end at top."""
        if bus.get("reversed"):
            return format_addr(bus["end"] - 1), format_addr(bus["start"])
        return format_addr(bus["start"]), format_addr(bus["end"] - 1)

    def _addr_lines_html(buses: list[dict], side: str, row: dict | None = None) -> str:
        """Build HTML for all address lines on one side (left or right). One pair (top, bottom) per bus.
        If row has both buses, each address span gets data-opposite-addr and data-opposite-label for hover."""
        if not buses:
            return ""
        lines: list[str] = []
        for bus in buses:
            top_a, bottom_a = _addr_at_top_bottom(bus)
            if bus.get("reversed"):
                top_num, bottom_num = bus["end"] - 1, bus["start"]
            else:
                top_num, bottom_num = bus["start"], bus["end"] - 1
            opp_top = _opposite_bus_addr(top_num, side, row) if row else None
            opp_bottom = _opposite_bus_addr(bottom_num, side, row) if row else None

            def _addr_span(addr: str, opp: tuple[str, str] | None) -> str:
                if opp:
                    oa, ol = opp
                    return f'<span class="addr" data-opposite-addr="{_html_attr_escape(oa)}" data-opposite-label="{_html_attr_escape(ol)}">{addr}</span>'
                return f'<span class="addr">{addr}</span>'

            if side == "left":
                lines.append(f'<div class="addr-line">{_addr_span(top_a, opp_top)}<span class="dash"></span></div>')
                lines.append(f'<div class="addr-line">{_addr_span(bottom_a, opp_bottom)}<span class="dash"></span></div>')
            else:
                lines.append(f'<div class="addr-line"><span class="dash"></span>{_addr_span(top_a, opp_top)}</div>')
                lines.append(f'<div class="addr-line"><span class="dash"></span>{_addr_span(bottom_a, opp_bottom)}</div>')
        return "\n".join(lines)

    # Minimum vertical gap between symbol lines (as fraction 0..1) so overlapping symbols stack
    _SYMBOL_LINE_GAP = 0.04
    # Minimum gap between segment used start/end address lines (col 2/4) so text stays readable
    _SEGMENT_ADDR_LINE_MIN_GAP = 0.05

    def _segment_addr_lines_html(
        strips: list[dict], side: str, row: dict | None = None, available_end_only: bool = False, row_for_opposite: dict | None = None
    ) -> str:
        """Col 1/5: available-end (origin+length) markers. Col 2/4: segment start and end only (with hover); never at boundary."""
        if not strips:
            return ""
        buses = row["left_buses"] if row and side == "left" else (row["right_buses"] if row and side == "right" else [])
        bus_start = buses[0]["start"] if buses else None
        bus_end = buses[0]["end"] if buses else None
        marker_keys: set[tuple[float, str]] = set()
        pairs: list[tuple[float, str, int | None, str]] = []
        for s in strips:
            t, ht = s["top_pct"], s["height_pct"]
            start_addr, end_addr = s.get("start_addr", ""), s.get("end_addr", "")
            start_num = s.get("start_num")
            end_num = s.get("end_num")
            if not available_end_only:
                if s.get("contains_start", True):
                    pairs.append((t, start_addr, start_num, ""))
                if s.get("contains_end", True):
                    pairs.append((t + ht, end_addr, end_num, ""))
            avail_end_pct = s.get("available_end_top_pct")
            avail_end_addr = s.get("available_end_addr")
            if avail_end_addr and avail_end_pct is not None and 0 <= avail_end_pct <= 1:
                pairs.append((avail_end_pct, avail_end_addr, None, " available-end"))
                marker_keys.add((round(avail_end_pct, 4), avail_end_addr))
        if available_end_only:
            pairs = [p for p in pairs if p[3] == " available-end"]
        else:
            pairs = [p for p in pairs if p[3] == ""]
            pairs = [p for p in pairs if (round(p[0], 4), p[1]) not in marker_keys]
            pairs = [p for p in pairs if not (p[2] is not None and (p[2] == bus_start or p[2] == bus_end))]
        if not pairs:
            return ""
        seen: set[tuple[float, str]] = set()
        unique: list[tuple[float, str, int | None, str]] = []
        for top_pct, addr, addr_num, cls in sorted(pairs, key=lambda x: x[0]):
            key = (round(top_pct, 4), addr)
            if key not in seen:
                seen.add(key)
                unique.append((top_pct, addr, addr_num, cls))
        row_opp = row if (row and row.get("iram_dram_offset") is not None) else row_for_opposite
        parts: list[str] = []
        for top_pct, addr, addr_num, line_cls in unique:
            opp = _opposite_bus_addr(addr_num, side, row_opp) if row_opp and addr_num is not None else None
            if opp:
                oa, ol = opp
                addr_span = f'<span class="addr" data-opposite-addr="{_html_attr_escape(oa)}" data-opposite-label="{_html_attr_escape(ol)}">{addr}</span>'
            else:
                addr_span = f'<span class="addr">{addr}</span>'
            half = SEGMENT_LINE_HEIGHT_PX // 2
            seg_style = f"top: calc({top_pct*100:.2f}% - {half}px); height: {SEGMENT_LINE_HEIGHT_PX}px;"
            div_class = "addr-line-segment" + line_cls
            if side == "left":
                parts.append(f'<div class="{div_class}" style="{seg_style}">{addr_span}<span class="dash"></span></div>')
            else:
                parts.append(f'<div class="{div_class}" style="{seg_style}"><span class="dash"></span>{addr_span}</div>')
        return "\n".join(parts)

    row_for_opposite = next((r for r in rows if r.get("iram_dram_offset") is not None), None)
    row_parts = []
    for i, r in enumerate(rows):
        h = heights[i]
        lb = r["left_buses"]
        rb = r["right_buses"]
        left_seg_addrs = _segment_addr_lines_html(r.get("left_segment_strips", []), "left", r, available_end_only=False, row_for_opposite=row_for_opposite)
        right_seg_addrs = _segment_addr_lines_html(r.get("right_segment_strips", []), "right", r, available_end_only=False, row_for_opposite=row_for_opposite)
        left_seg_addrs_outer = _segment_addr_lines_html(r.get("left_segment_strips", []), "left", r, available_end_only=True)
        right_seg_addrs_outer = _segment_addr_lines_html(r.get("right_segment_strips", []), "right", r, available_end_only=True)

        # Outer columns: available end (origin+length) markers only, so they don't interfere with segment addresses
        segment_sym_left_cell = (
            f'<div class="addr-side segment-col sym-col segment-left" style="height:{h}px">'
            f"{left_seg_addrs_outer}"
            f"</div>"
        )
        segment_sym_right_cell = (
            f'<div class="addr-side segment-col sym-col segment-right" style="height:{h}px">'
            f"{right_seg_addrs_outer}"
            f"</div>"
        )
        segment_left_cell = (
            f'<div class="addr-side segment-col segment-left" style="height:{h}px">'
            f"{left_seg_addrs}"
            f"</div>"
        )
        segment_right_cell = (
            f'<div class="addr-side segment-col segment-right" style="height:{h}px">'
            f"{right_seg_addrs}"
            f"</div>"
        )

        label = f"{r['name']} ({format_size_kb(r['size'])})"
        left_strips = r.get("left_segment_strips", [])
        right_strips = r.get("right_segment_strips", [])

        _SECTION_SEP = "\u241e"  # Unicode record separator; safe in HTML attr and unlikely in section names

        def _strip_div(s: dict) -> str:
            name = s.get("name", "?")
            start_a = s.get("start_addr", "")
            end_a = s.get("end_addr", "")
            start_sym = s.get("start_sym", "")
            end_sym = s.get("end_sym", "")
            sections = s.get("input_sections", [])
            sections_attr = _html_attr_escape(_SECTION_SEP.join(sections)) if sections else ""
            avail = s.get("available_bytes")
            avail_end = s.get("available_end_addr")
            used = s.get("used_bytes")
            used_block = s.get("used_in_this_block")
            data_avail = f' data-seg-available-bytes="{avail}"' if avail is not None else ""
            data_avail_end = f' data-seg-available-end="{_html_attr_escape(avail_end or "")}"'
            data_used = f' data-seg-used-bytes="{used}"' if used is not None else ""
            data_used_block = f' data-seg-used-in-block="{used_block}"' if used_block is not None else ""
            return (
                f'<div class="mem-block-segment" style="top:{s["top_pct"]*100:.2f}%; height:{s["height_pct"]*100:.2f}%; background:{s["color"]};"'
                f' data-seg-name="{_html_attr_escape(name)}"'
                f' data-seg-start="{_html_attr_escape(start_a)}" data-seg-end="{_html_attr_escape(end_a)}"'
                f' data-seg-start-sym="{_html_attr_escape(start_sym)}" data-seg-end-sym="{_html_attr_escape(end_sym)}"'
                f' data-seg-sections="{sections_attr}"'
                f"{data_avail}{data_avail_end}{data_used}{data_used_block}>"
                f"</div>"
            )

        left_strips_html = "".join(_strip_div(s) for s in left_strips)
        right_strips_html = "".join(_strip_div(s) for s in right_strips)
        # Start and end addresses inside block (left and right side), small font
        left_top_a, left_bottom_a = _addr_at_top_bottom(lb[0]) if lb else ("", "")
        right_top_a, right_bottom_a = _addr_at_top_bottom(rb[0]) if rb else ("", "")
        def _addr_esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") if s else ""

        left_addr_html = (
            f'<div class="mem-block-addr mem-block-addr-left">'
            f'<span class="mem-block-addr-line">{_addr_esc(left_top_a)}</span>'
            f'<span class="mem-block-addr-line">{_addr_esc(left_bottom_a)}</span>'
            f"</div>"
        ) if (left_top_a or left_bottom_a) else ""
        right_addr_html = (
            f'<div class="mem-block-addr mem-block-addr-right">'
            f'<span class="mem-block-addr-line">{_addr_esc(right_top_a)}</span>'
            f'<span class="mem-block-addr-line">{_addr_esc(right_bottom_a)}</span>'
            f"</div>"
        ) if (right_top_a or right_bottom_a) else ""
        block_cell = (
            f'<div class="mem-block" style="height:{h}px; width:{BLOCK_WIDTH_PX}px">'
            f'<div class="mem-block-segments mem-block-segments-left">'
            f"{left_strips_html}"
            f"</div>"
            f'<div class="mem-block-segments mem-block-segments-right">'
            f"{right_strips_html}"
            f"</div>"
            f"{left_addr_html}"
            f"{right_addr_html}"
            f'<span class="mem-label">{label}</span>'
            f"</div>"
        )

        # Separator before this memory (gap + optional label)
        sep_before = r.get("separator_before")
        if sep_before:
            label_html = ""
            if sep_before.get("name"):
                n = sep_before["name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                label_html = f'<span class="memory-separator-label">{n}</span>'
            row_parts.append(f'<div class="memory-separator">{label_html}</div>')
            # After separator: header row with bus names for the rows that follow
            left_bus_lbl = r["left_buses"][0].get("name", "IRAM") if r["left_buses"] else "Segment"
            right_bus_lbl = r["right_buses"][0].get("name", "DRAM") if r["right_buses"] else "Segment"
            left_esc = left_bus_lbl.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            right_esc = right_bus_lbl.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            row_parts.append(
                f'<div class="bus-labels">'
                f'<span class="label-sym-left">Symbol</span>'
                f'<span class="label-seg-left">{left_esc}</span>'
                f'<span class="label-center"></span>'
                f'<span class="label-seg-right">{right_esc}</span>'
                f'<span class="label-sym-right">Symbol</span>'
                f'</div>'
            )
        # Column order: 1=left markers | 2=left bus | 3=center | 4=right bus | 5=right markers
        row_parts.append(f'<div class="memory-row">{segment_sym_left_cell}{segment_left_cell}{block_cell}{segment_right_cell}{segment_sym_right_cell}</div>')

    rows_html = "\n    ".join(row_parts)

    # Top header: use bus names from first row that has each bus (first row may have only left, e.g. SRAM0)
    first_left_bus = "Segment"
    first_right_bus = "Segment"
    for r in rows:
        if r["left_buses"] and first_left_bus == "Segment":
            first_left_bus = r["left_buses"][0].get("name", "IRAM")
        if r["right_buses"] and first_right_bus == "Segment":
            first_right_bus = r["right_buses"][0].get("name", "DRAM")
        if first_left_bus != "Segment" and first_right_bus != "Segment":
            break
    first_left_esc = first_left_bus.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    first_right_esc = first_right_bus.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Address conversion section (offset and C macros when both buses used; supports IRAM/DRAM and IROM/DROM)
    conversion_parts: list[str] = []
    for r in rows:
        offset = r.get("iram_dram_offset")
        if offset is None:
            continue
        name = r["name"]
        prefix = _c_id(name)
        size = r["size"]
        left_start = r["iram_start"]
        right_start = r["dram_start"]
        left_name = r.get("left_bus_name", "IRAM")
        right_name = r.get("right_bus_name", "DRAM")
        reversed_dir = r.get("reversed", False)
        # Offset label: IROM-DROM uses irom_drom_offset = IROM_start - DROM_start; IRAM-DRAM uses iram_dram_offset
        is_irom_drom = left_name == "IROM" and right_name == "DROM"
        offset_label = "irom_drom_offset" if is_irom_drom else "iram_dram_offset"
        conversion_parts.append(f'<div class="conversion-block">')
        conversion_parts.append(f'<div class="conversion-title">{name} ({left_name} ↔ {right_name})</div>')
        conversion_parts.append(
            f'<div class="conversion-offset">{offset_label} = {left_name}_start - {right_name}_start = {format_addr(offset)} ({offset})</div>'
        )
        if reversed_dir:
            conversion_parts.append(
                f'<pre class="conversion-macros">'
                f'/* Convert {left_name} address to its {right_name} counterpart in {name} memory */\n'
                f'#define {prefix}_{left_name.upper()}_{right_name.upper()}_CALC(addr_{left_name.lower()}) '
                f'({prefix}_SIZE - (addr_{left_name.lower()} - {prefix}_{left_name.upper()}_START) + {prefix}_{right_name.upper()}_START)\n\n'
                f'/* Convert {right_name} address to its {left_name} counterpart in {name} memory */\n'
                f'#define {prefix}_{right_name.upper()}_{left_name.upper()}_CALC(addr_{right_name.lower()}) '
                f'({prefix}_SIZE - (addr_{right_name.lower()} - {prefix}_{right_name.upper()}_START) + {prefix}_{left_name.upper()}_START)</pre>'
            )
            conversion_parts.append(
                f'<div class="conversion-constants">'
                f'{prefix}_{left_name.upper()}_START = {format_addr(left_start)}, '
                f'{prefix}_{right_name.upper()}_START = {format_addr(right_start)}, '
                f'{prefix}_SIZE = {size}</div>'
            )
        else:
            conversion_parts.append(
                f'<pre class="conversion-macros">'
                f'/* Convert {left_name} address to its {right_name} counterpart in {name} memory */\n'
                f'#define {prefix}_{left_name.upper()}_{right_name.upper()}_CALC(addr_{left_name.lower()}) '
                f'(addr_{left_name.lower()} - ({prefix}_{left_name.upper()}_START - {prefix}_{right_name.upper()}_START))\n\n'
                f'/* Convert {right_name} address to its {left_name} counterpart in {name} memory */\n'
                f'#define {prefix}_{right_name.upper()}_{left_name.upper()}_CALC(addr_{right_name.lower()}) '
                f'(addr_{right_name.lower()} + ({prefix}_{left_name.upper()}_START - {prefix}_{right_name.upper()}_START))</pre>'
            )
            conversion_parts.append(
                f'<div class="conversion-constants">'
                f'{prefix}_{left_name.upper()}_START = {format_addr(left_start)}, '
                f'{prefix}_{right_name.upper()}_START = {format_addr(right_start)}</div>'
            )
        conversion_parts.append("</div>")
    if conversion_parts:
        conversion_section = (
            '<div class="address-conversion">'
            "<h2>Address conversion (dual-bus: IRAM↔DRAM / IROM↔DROM)</h2>\n"
            + "\n".join(conversion_parts)
            + "</div>"
        )
    else:
        conversion_section = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font: {LABEL_FONT}; margin: 16px; background: #fff; color: #000; }}
    h1 {{ font-size: 1.1rem; font-weight: bold; text-align: center; margin-bottom: 16px; }}
    .diagram {{ display: flex; flex-direction: row; align-items: flex-start; gap: 0; }}
    .bus-labels {{ display: flex; flex-direction: row; margin-bottom: 4px; padding-bottom: 4px; border-bottom: 1px solid #333; }}
    .bus-labels .label-sym-left {{ width: 100px; text-align: left; }}
    .bus-labels .label-seg-left {{ width: 100px; text-align: left; }}
    .bus-labels .label-center {{ width: {BLOCK_WIDTH_PX}px; }}
    .bus-labels .label-seg-right {{ width: 100px; text-align: right; }}
    .bus-labels .label-sym-right {{ width: 100px; text-align: right; }}
    .memory-rows {{ display: flex; flex-direction: column; overflow: visible; }}
    .memory-separator {{ margin-top: 20px; padding-top: 12px; width: 100%; text-align: center; border-top: 1px solid #999; }}
    .memory-separator-label {{ font-weight: bold; font-size: 12px; }}
    .memory-row {{ display: flex; flex-direction: row; align-items: stretch; overflow: visible; }}
    .addr-side {{ position: relative; width: 100px; display: flex; flex-direction: column; justify-content: space-between; flex-shrink: 0; overflow: visible; }}
    .segment-col {{ min-width: 100px; }}
    .segment-col.segment-left {{ text-align: left; }}
    .segment-col.segment-left .addr-line-segment {{ justify-content: flex-start; }}
    .segment-col.segment-right {{ text-align: right; }}
    .segment-col.segment-right .addr-line-segment {{ justify-content: flex-end; }}
    .segment-left .addr-line-segment .addr {{ margin-right: 4px; }}
    .segment-right .addr-line-segment .addr {{ margin-left: 4px; }}
    .segment-col .addr-line-segment .addr {{ font-size: 9px; }}
    /* Strip edge, dash line, and address on same horizontal: row center = top_pct (strip edge), content centered in row */
    .addr-line-segment {{ position: absolute; left: 0; right: 0; display: flex; align-items: center; box-sizing: border-box; }}
    .segment-col .addr-line-segment .addr {{ margin-top: 0; line-height: 1; }}
    .addr-left .addr-line-segment .addr {{ margin-right: 4px; }}
    .addr-right .addr-line-segment .addr {{ margin-left: 4px; }}
    .addr-left {{ text-align: left; }}
    .addr-left .addr-line {{ justify-content: flex-start; }}
    .addr-right {{ text-align: right; }}
    .addr-right .addr-line {{ justify-content: flex-end; }}
    .addr-line {{ display: flex; align-items: center; width: 100%; }}
    .addr-line.top {{ align-self: flex-start; }}
    .addr-line.bottom {{ align-self: flex-end; }}
    .addr {{ font-family: ui-monospace, monospace; font-size: 11px; white-space: nowrap; }}
    .addr-sym {{ font-size: 10px; color: #555; font-family: ui-monospace, monospace; white-space: nowrap; }}
    .addr-left .addr {{ margin-right: 4px; }}
    .addr-right .addr {{ margin-left: 4px; }}
    .dash {{ flex: 1; border-bottom: 1px dashed #333; min-width: 8px; }}
    /* Segment dash between address and block; dash line at vertical center of row (aligns with strip edge). */
    .segment-col .addr-line-segment .dash {{ flex: 1; min-width: 8px; align-self: center; height: 0; border-bottom: 1px dashed #333; pointer-events: none; }}
    .addr-line-segment.available-end .addr {{ color: #0066aa; font-weight: 500; }}
    .mem-block {{
      position: relative;
      width: {BLOCK_WIDTH_PX}px;
      min-height: {MIN_BLOCK_HEIGHT_PX}px;
      background: #e8e8e8;
      outline: 1px solid #000;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}
    /* Strip container: explicit height 100%% so .mem-block-segment percentage heights resolve in every row (including after separator) */
    .mem-block-segments {{
      position: absolute;
      top: 0;
      bottom: 0;
      width: {SEGMENT_STRIP_WIDTH_PX}px;
      height: 100%;
      z-index: 1;
    }}
    .mem-block-segments-left {{ left: 0; }}
    .mem-block-segments-right {{ right: 0; height: 100%; }}
    .segment-col.segment-right .addr-line-segment {{ align-items: center; }}
    .mem-block-addr {{
      position: absolute;
      top: 0;
      bottom: 0;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 4px 10px;
      font-family: ui-monospace, monospace;
      font-size: 9px;
      color: #333;
      pointer-events: none;
      z-index: 0;
    }}
    .mem-block-addr-left {{ left: {SEGMENT_STRIP_WIDTH_PX}px; text-align: left; max-width: 45%; }}
    .mem-block-addr-right {{ right: {SEGMENT_STRIP_WIDTH_PX}px; text-align: right; max-width: 45%; }}
    .mem-block-addr-line {{ white-space: nowrap; }}
    .mem-block-segment {{
      position: absolute;
      left: 0;
      width: 100%;
      min-height: 2px;
      pointer-events: auto;
      cursor: pointer;
    }}
    .seg-tooltip {{
      position: fixed;
      z-index: 1000;
      display: none;
      max-width: 320px;
      max-height: 80vh;
      background: #fff;
      border: 1px solid #333;
      border-radius: 4px;
      box-shadow: 4px 4px 8px rgba(0,0,0,0.2);
      font: 11px ui-monospace, monospace;
      flex-direction: column;
    }}
    .seg-tooltip .seg-tooltip-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 8px 12px;
      border-bottom: 1px solid #ccc;
      flex-shrink: 0;
      background: #f0f0f0;
      border-radius: 4px 4px 0 0;
    }}
    .seg-tooltip .seg-tooltip-title {{ font-weight: bold; margin: 0; }}
    .seg-tooltip .seg-tooltip-close {{
      cursor: pointer;
      padding: 2px 8px;
      border: 1px solid #999;
      border-radius: 3px;
      background: #fff;
      font: 11px ui-monospace, monospace;
    }}
    .seg-tooltip .seg-tooltip-close:hover {{ background: #e0e0e0; }}
    .seg-tooltip .seg-tooltip-body {{
      padding: 10px 12px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }}
    .seg-tooltip .seg-tooltip-name {{ margin-bottom: 6px; }}
    .seg-tooltip .seg-tooltip-addr {{ margin: 4px 0; }}
    .seg-tooltip .seg-tooltip-sections {{ margin-top: 6px; border-top: 1px solid #ccc; padding-top: 6px; }}
    .addr[data-opposite-addr] {{ cursor: help; }}
    .segment-col .addr-line-segment .addr[data-opposite-addr] {{ position: relative; z-index: 1; }}
    .opposite-addr-tooltip {{
      position: fixed;
      z-index: 1001;
      display: none;
      padding: 6px 10px;
      background: #fff;
      border: 1px solid #333;
      border-radius: 4px;
      box-shadow: 2px 2px 6px rgba(0,0,0,0.2);
      font: 11px ui-monospace, monospace;
      white-space: nowrap;
    }}
    .mem-label {{ font-size: 12px; font-weight: normal; position: relative; z-index: 1; }}
    .address-conversion {{ margin-top: 24px; }}
    .address-conversion h2 {{ font-size: 1rem; margin-bottom: 8px; }}
    .conversion-block {{ margin-bottom: 16px; padding: 12px; background: #f5f5f5; border: 1px solid #ccc; font-family: ui-monospace, monospace; font-size: 11px; }}
    .conversion-title {{ font-weight: bold; margin-bottom: 6px; }}
    .conversion-offset {{ margin-bottom: 8px; }}
    .conversion-macros {{ margin: 8px 0; padding: 8px; background: #fff; border: 1px solid #ddd; overflow-x: auto; white-space: pre; }}
    .conversion-constants {{ margin-top: 6px; color: #555; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="bus-labels">
    <span class="label-sym-left">Symbol</span>
    <span class="label-seg-left">{first_left_esc}</span>
    <span class="label-center"></span>
    <span class="label-seg-right">{first_right_esc}</span>
    <span class="label-sym-right">Symbol</span>
  </div>
  <div class="diagram">
    <div class="memory-rows">
    {rows_html}
    </div>
  </div>
  {conversion_section}
  <div id="seg-tooltip" class="seg-tooltip" aria-hidden="true"></div>
  <div id="opposite-addr-tooltip" class="opposite-addr-tooltip" aria-hidden="true"></div>
  <script>
(function() {{
  var tip = document.getElementById("seg-tooltip");
  var strips = document.querySelectorAll(".mem-block-segment");
  var pinned = false;
  var oppTip = document.getElementById("opposite-addr-tooltip");
  function showOppTip(ev, el) {{
    var label = el.getAttribute("data-opposite-label") || "";
    var addr = el.getAttribute("data-opposite-addr") || "";
    oppTip.textContent = label + ": " + addr;
    oppTip.style.display = "block";
    oppTip.style.left = (ev.clientX + 10) + "px";
    oppTip.style.top = (ev.clientY + 10) + "px";
    oppTip.setAttribute("aria-hidden", "false");
  }}
  var addrsWithOpp = document.querySelectorAll(".addr[data-opposite-addr]");
  for (var i = 0; i < addrsWithOpp.length; i++) {{
    if (addrsWithOpp[i].closest(".segment-col")) continue;
    addrsWithOpp[i].addEventListener("mouseenter", function(ev) {{ showOppTip(ev, this); }});
    addrsWithOpp[i].addEventListener("mouseleave", function() {{
      oppTip.style.display = "none";
      oppTip.setAttribute("aria-hidden", "true");
    }});
  }}
  var segmentLines = document.querySelectorAll(".segment-col .addr-line-segment");
  for (var j = 0; j < segmentLines.length; j++) {{
    (function(line) {{
      var addrEl = line.querySelector(".addr[data-opposite-addr]");
      if (!addrEl) return;
      line.addEventListener("mouseenter", function(ev) {{ showOppTip(ev, addrEl); }});
      line.addEventListener("mouseleave", function() {{
        oppTip.style.display = "none";
        oppTip.setAttribute("aria-hidden", "true");
      }});
    }})(segmentLines[j]);
  }}
  function esc(s) {{
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }}
  function fmtSize(bytes) {{
    if (bytes === "" || bytes === null || bytes === undefined) return null;
    var b = parseInt(bytes, 10);
    if (isNaN(b)) return null;
    var kb = (b / 1024).toFixed(2).replace(/\\.?0+$/, "");
    return kb + " kB (" + b + " Bytes)";
  }}
  function show(ev, name, start, end, startSym, endSym, sections, availableBytes, availableEndAddr, usedBytes, usedInBlock) {{
    var sep = "\\u241e";
    var lines = sections ? sections.split(sep).filter(Boolean) : [];
    var bodyHtml = "";
    if (name) bodyHtml += "<div class=\\"seg-tooltip-name\\"><strong>Seg. name:</strong> " + esc(name) + "</div>";
    var availStr = fmtSize(availableBytes);
    if (availStr) bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Segment Length:</strong> " + esc(availStr) + "</div>";
    bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Seg. available end:</strong> " + esc(availableEndAddr || "\\u2014") + "</div>";
    var usedStr = fmtSize(usedBytes);
    if (usedStr) bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Used:</strong> " + esc(usedStr) + "</div>";
    var usedBlockStr = fmtSize(usedInBlock);
    if (usedBlockStr) bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Used in this block:</strong> " + esc(usedBlockStr) + "</div>";
    bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Used start:</strong> " + esc(start) + (startSym ? " (" + esc(startSym) + ")" : "") + "</div>";
    bodyHtml += "<div class=\\"seg-tooltip-addr\\"><strong>Used end:</strong> " + esc(end) + (endSym ? " (" + esc(endSym) + ")" : "") + "</div>";
    if (lines.length) {{
      bodyHtml += "<div class=\\"seg-tooltip-sections\\"><strong>Input sections:</strong><br>";
      for (var i = 0; i < lines.length; i++) bodyHtml += esc(lines[i]) + "<br>";
      bodyHtml += "</div>";
    }} else {{
      bodyHtml += "<div class=\\"seg-tooltip-sections\\"><strong>Input sections:</strong> (none)</div>";
    }}
    tip.innerHTML = "<div class=\\"seg-tooltip-header\\"><span class=\\"seg-tooltip-title\\">Segment</span><button type=\\"button\\" class=\\"seg-tooltip-close\\" aria-label=\\"Close\\">Close</button></div><div class=\\"seg-tooltip-body\\">" + bodyHtml + "</div>";
    tip.style.display = "flex";
    tip.style.left = (ev.clientX + 12) + "px";
    tip.style.top = (ev.clientY + 12) + "px";
    tip.setAttribute("aria-hidden", "false");
    tip.querySelector(".seg-tooltip-close").onclick = function(e) {{
      e.stopPropagation();
      hide();
    }};
  }}
  function hide() {{
    pinned = false;
    tip.style.display = "none";
    tip.setAttribute("aria-hidden", "true");
  }}
  for (var i = 0; i < strips.length; i++) {{
    (function(el) {{
      el.addEventListener("mouseenter", function(ev) {{
        show(ev, el.getAttribute("data-seg-name") || "", el.getAttribute("data-seg-start") || "", el.getAttribute("data-seg-end") || "", el.getAttribute("data-seg-start-sym") || "", el.getAttribute("data-seg-end-sym") || "", el.getAttribute("data-seg-sections") || "", el.getAttribute("data-seg-available-bytes") || "", el.getAttribute("data-seg-available-end") || "", el.getAttribute("data-seg-used-bytes") || "", el.getAttribute("data-seg-used-in-block") || "");
      }});
      el.addEventListener("mouseleave", function() {{
        if (!pinned) hide();
      }});
      el.addEventListener("click", function(ev) {{
        ev.preventDefault();
        ev.stopPropagation();
        pinned = true;
        show(ev, el.getAttribute("data-seg-name") || "", el.getAttribute("data-seg-start") || "", el.getAttribute("data-seg-end") || "", el.getAttribute("data-seg-start-sym") || "", el.getAttribute("data-seg-end-sym") || "", el.getAttribute("data-seg-sections") || "", el.getAttribute("data-seg-available-bytes") || "", el.getAttribute("data-seg-available-end") || "", el.getAttribute("data-seg-used-bytes") || "", el.getAttribute("data-seg-used-in-block") || "");
      }});
    }})(strips[i]);
  }}
  document.addEventListener("click", function(ev) {{
    if (pinned && tip.style.display === "flex" && !tip.contains(ev.target)) hide();
  }});
}})();
  </script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Memory usage visualization: render SoC memory schema (and optionally map file) to HTML."
    )
    parser.add_argument("--soc", metavar="NAME", help="SoC type (e.g. esp32s3). Loads soc/<name>.yaml.")
    parser.add_argument("-o", "--output", metavar="FILE", default="memwiz_out.html", help="Output HTML file.")
    parser.add_argument("--map", metavar="FILE", help="Linker map file (optional; for later phases).")
    args = parser.parse_args()

    if not args.soc:
        parser.error("Provide --soc NAME (e.g. --soc esp32s3).")

    script_dir = Path(__file__).resolve().parent
    soc_path = script_dir / "soc" / f"{args.soc}.yaml"
    if not soc_path.is_file():
        print(f"Error: SoC schema not found: {soc_path}", file=sys.stderr)
        return 1

    memories = load_soc_schema(soc_path)
    if not memories:
        print("Error: No memories defined in schema.", file=sys.stderr)
        return 1

    resolved_segments: list[dict] = []
    memory_config: dict[str, dict] = {}
    if args.map:
        map_path = Path(args.map)
        if not map_path.is_file():
            print(f"Error: Map file not found: {map_path}", file=sys.stderr)
            return 1
        memory_config = parse_map_memory_config(map_path)
        platform_path = script_dir / "platform" / "zephyr.yaml"
        if not platform_path.is_file():
            print(f"Warning: Platform config not found: {platform_path}, skipping segments.", file=sys.stderr)
        else:
            symbols = parse_map_symbols(map_path)
            platform_segments = load_platform_segments(platform_path)
            resolved_segments = resolve_segments(platform_segments, symbols)
            map_sections = parse_map_sections(map_path)
            segment_input_sections(resolved_segments, map_sections)

    out_path = Path(args.output)
    out_path.write_text(
        render_soc_html(
            memories,
            args.soc,
            resolved_segments=resolved_segments,
            memory_config=memory_config,
        ),
        encoding="utf-8",
    )
    print(f"Wrote: {out_path.absolute()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
