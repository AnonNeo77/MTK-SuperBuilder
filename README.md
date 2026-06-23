# MTK-SuperBuilder

MTK-SuperBuilder is a standalone Python CLI utility for building and unpacking Android Dynamic Partition `super.img` files using OEM `super_def.json` layouts.

The tool automatically generates the required `lpmake` arguments from the provided configuration and partition images, allowing firmware layouts to be rebuilt without manually constructing complex commands.

## Features

* Build Android `super.img` files from OEM `super_def.json` layouts
* Automatic `lpmake` command generation
* Validate partition image paths and layout definitions
* Support A/B Dynamic Partition configurations
* Optional empty-slot partition creation
* Import and unpack existing `super.img` files
* Generate reusable `build.sh` scripts
* Lightweight single-file Python utility

## Requirements

The following tools must be available in your system `PATH`:

* `lpmake`
* `lpunpack`

Optional:

* `lpdump`

Python:

* Python 3.11+

## Installation

Clone the repository:

```bash
git clone https://github.com/AnonNeo77/MTK-SuperBuilder.git
cd MTK-SuperBuilder
```

Or make the script globally accessible:

```bash
chmod +x superbuilder.py
sudo cp superbuilder.py /usr/local/bin/superbuilder
```

## Firmware Layout

Expected directory structure:

```text
firmware/
├── IMAGES/
│   ├── system.img
│   ├── vendor.img
│   ├── product.img
│   └── ...
└── super_def.json
```

The tool also supports OEM naming variations such as:

```text
super_def.json
super_def.00011011.json
super_def.*.json
```

## Usage

### Display Layout Information

```bash
superbuilder info <super_def.json>
```

Shows:

* Super partition size
* Metadata information
* Dynamic partition groups
* Partition definitions

### Validate Layout

```bash
superbuilder validate <super_def.json>
```

Checks:

* Configuration validity
* Partition image existence
* Partition size constraints
* Group definitions

Create empty A/B slot entries when required:

```bash
superbuilder validate <super_def.json> --create-empty-slots
```

### Build super.img

```bash
superbuilder build <super_def.json> -o super.img
```

Print generated `lpmake` command:

```bash
superbuilder build <super_def.json> -o super.img --print-cmd
```

Create empty slot entries during build:

```bash
superbuilder build <super_def.json> -o super.img --create-empty-slots
```

Dry-run without executing `lpmake`:

```bash
superbuilder build <super_def.json> -o super.img --dry-run
```

### Import Existing super.img

```bash
superbuilder import super.img -o workspace
```

This will:

* Extract partition images
* Generate a compatible `super_def.json`
* Create a rebuildable workspace

Output example:

```text
workspace/
├── IMAGES/
│   ├── system.img
│   ├── vendor.img
│   └── ...
└── super_def.json
```

## Example

Build a super.img :

```bash
superbuilder build . -o super.img --create-empty-slots --print-cmd
```
