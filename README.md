# memwiz

Memory usage visualization tool for SoC memory layouts. Renders the memory schema (and optionally linker map data) to a single HTML file that you can open in a browser.

## General description

memwiz reads a SoC memory schema from YAML and produces an HTML diagram of memory blocks (e.g. SRAM0, SRAM1, CACHE) with addresses on the left and right. Optionally, it can read a linker map file and overlay **linker segments** (e.g. `iram0_0_seg`, `dram0_0_seg`) as colored strips on the blocks, with segment start/end addresses and hover tooltips (segment name, length, used bytes, input sections, opposite-bus address).

- **Memory blocks**: Each block is a row with start address at the top and address growing downward; fixed width and minimum height for visibility.
- **Columns**: Left markers (1), left bus addresses (2), center block with strips (3), right bus addresses (4), right markers (5). Segment start/end appear in columns 2 and 4 where the red stripe starts/ends; markers (e.g. available end) in columns 1 and 5.
- **Dual-bus rows**: For rows with both IRAM and DRAM (or IROM and DROM), address labels can show the opposite bus address on hover (e.g. IRAM address → DRAM*).

**Requirements**: Python 3, PyYAML (`pip install pyyaml`).

## Usage

Run from the `memwiz` directory (or ensure `soc/` and `platform/` exist relative to the script).

### Basic (schema only)

Generate HTML from the SoC schema only (no segments, no map data):

```bash
python memwiz.py --soc esp32s3
```

Output is written to `memwiz_out.html` by default.

### With linker map (segments and addresses)

To overlay segments and addresses from a linker map file:

```bash
python memwiz.py --soc esp32s3 --map path/to/zephyr.map
```

This will:

- Parse the map’s **Memory Configuration** table (segment origin and length).
- Resolve segment ranges from the platform config (`platform/zephyr.yaml`) and map symbols.
- Draw segment strips and show segment start/end and marker addresses in the diagram.
- Attach hover tooltips on segment strips with segment name, length, used bytes, and input sections.

### Custom output file

Specify the output HTML file with `-o` or `--output`:

```bash
python memwiz.py --soc esp32s3 --map zephyr.map -o memory.html
```

### Summary of options

| Option | Description |
|--------|-------------|
| `--soc NAME` | **Required.** SoC type (e.g. `esp32s3`). Loads `soc/<name>.yaml`. |
| `-o`, `--output FILE` | Output HTML path (default: `memwiz_out.html`). |
| `--map FILE` | Linker map file (optional). Enables segments and address overlay. |

### Examples

```bash
# Schema only
python memwiz.py --soc esp32s3 -o soc_only.html

# Full diagram with map
python memwiz.py --soc esp32s3 --map build/zephyr/zephyr.map -o mem.html
```

Then open the generated HTML file in a browser to view and interact with the diagram.
