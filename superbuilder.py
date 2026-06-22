#!/usr/bin/env python3
"""
MTK-SuperBuilder: A streamlined CLI utility to build and unpack Android super.img
layouts using lpmake and lpunpack .
"""

import os
import sys
import json
import shutil
import struct
import logging
import subprocess
import argparse
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional, Any, Set

# ==============================================================================
# 1. Custom Exceptions
# ==============================================================================

class SuperBuilderError(Exception):
    """Base exception for all MTK-SuperBuilder errors."""
    pass

class InvalidSuperDefError(SuperBuilderError):
    """Raised when the super_def.json configuration is invalid or missing keys."""
    pass

class MissingImageError(SuperBuilderError):
    """Raised when a partition image file cannot be located on disk."""
    pass

class GroupSizeExceededError(SuperBuilderError):
    """Raised when the cumulative size of partitions in a group exceeds limits."""
    pass

class LpmakeNotFoundError(SuperBuilderError):
    """Raised when an Android utility binary (lpmake, lpunpack) is missing."""
    pass

class SparseConversionError(SuperBuilderError):
    """Raised when conversion of sparse images fails."""
    pass


# ==============================================================================
# 2. Data Models
# ==============================================================================

@dataclass
class BlockDevice:
    name: str
    size: int
    alignment: int = 1048576
    block_size: int = 4096

@dataclass
class PartitionGroup:
    name: str
    maximum_size: int

@dataclass
class Partition:
    name: str
    group_name: str
    path: Optional[str] = None
    size: int = 0
    is_empty: bool = False
    attributes: str = "readonly"

@dataclass
class SuperDef:
    metadata_size: int
    metadata_slots: int
    block_devices: List[BlockDevice] = field(default_factory=list)
    groups: List[PartitionGroup] = field(default_factory=list)
    partitions: List[Partition] = field(default_factory=list)

    def find_group(self, group_name: str) -> Optional[PartitionGroup]:
        for group in self.groups:
            if group.name == group_name:
                return group
        return None

    def find_partition(self, part_name: str) -> Optional[Partition]:
        for part in self.partitions:
            if part.name == part_name:
                return part
        return None


# ==============================================================================
# 3. System & Sparse Utilities
# ==============================================================================

logger = logging.getLogger("SuperBuilder")

def setup_logging(verbose: bool = False) -> None:
    """Configures clean console logging matching native Android tool outputs."""
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler()
    
    class CustomFormatter(logging.Formatter):
        def format(self, record):
            if record.levelno == logging.INFO:
                prefix = "[INFO]"
            elif record.levelno == logging.WARNING:
                prefix = "[WARNING]"
            elif record.levelno >= logging.ERROR:
                prefix = "[ERROR]"
            elif record.levelno == logging.DEBUG:
                prefix = "[DEBUG]"
            else:
                prefix = "[SYSTEM]"
            
            msg = record.getMessage()
            if msg.startswith("["):
                return msg
            return f"{prefix} {msg}"

    handler.setFormatter(CustomFormatter())
    logging.root.handlers = [handler]
    logging.root.setLevel(level)

def run_command(cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
    """Runs system commands safely and returns code, stdout, and stderr."""
    logger.debug(f"Executing: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            check=False
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as e:
        raise LpmakeNotFoundError(f"Command execution failed: {cmd[0]} not found in PATH.") from e

def check_binary(binary_name: str) -> Path:
    """Checks if an external Android system utility binary exists in $PATH."""
    path = shutil.which(binary_name)
    if not path:
        raise LpmakeNotFoundError(
            f"{binary_name} binary not found in PATH. Ensure android-platform-tools "
            "or equivalent packages are installed."
        )
    return Path(path)

def is_sparse_image(filepath: Path) -> bool:
    """Checks if an image matches the Android sparse file magic header (0xED26FF3A)."""
    if not filepath.exists() or filepath.is_dir() or filepath.stat().st_size < 28:
        return False
    try:
        with open(filepath, 'rb') as f:
            magic = f.read(4)
            if len(magic) < 4:
                return False
            return struct.unpack('<I', magic)[0] == 0xED26FF3A
    except Exception as e:
        logger.debug(f"Failed to read magic headers for {filepath.name}: {e}")
        return False

def get_unsparsed_size(filepath: Path) -> int:
    """Parses Android sparse headers to extract raw size without unpacking."""
    if not is_sparse_image(filepath):
        return filepath.stat().st_size

    try:
        with open(filepath, 'rb') as f:
            header = f.read(28)
            if len(header) < 28:
                return filepath.stat().st_size
            
            magic, _, _, _, _, block_size, total_blocks, _, _ = struct.unpack('<IHHHHIIII', header)
            if magic == 0xED26FF3A:
                return block_size * total_blocks
    except Exception as e:
        logger.warning(f"Error parsing sparse metadata on {filepath.name}: {e}. Falling back to actual size.")
    
    return filepath.stat().st_size

def convert_sparse_to_raw(sparse_path: Path, output_path: Path) -> Path:
    """Converts an Android sparse image back to raw using simg2img."""
    check_binary("simg2img")
    logger.info(f"[BUILD] Unsparsing {sparse_path.name} to raw format...")
    ret, _, err = run_command(["simg2img", str(sparse_path), str(output_path)])
    if ret != 0:
        raise SparseConversionError(f"Failed to convert sparse image {sparse_path.name}. stderr: {err}")
    return output_path


# ==============================================================================
# 4. JSON Parser
# ==============================================================================

class SuperDefParser:
    @staticmethod
    def parse(config_path: Path) -> SuperDef:
        """Loads and translates super_def.json configurations into SuperDef models."""
        target_file = config_path
        
        if target_file.is_dir():
            possible_configs = list(target_file.glob("super_def*.json"))
            if not possible_configs:
                raise InvalidSuperDefError(f"No super_def*.json config files found inside: {target_file}")
            
            standard_file = target_file / "super_def.json"
            target_file = standard_file if standard_file.exists() else possible_configs[0]

        if not target_file.exists():
            possible_configs = list(target_file.parent.glob("super_def*.json"))
            if possible_configs:
                target_file = possible_configs[0]
            else:
                raise InvalidSuperDefError(f"Configuration file not found: {target_file}")
            
        logger.info(f"[INFO] Using configuration layout: {target_file.name}")
            
        try:
            with open(target_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidSuperDefError(f"JSON parsing error inside {target_file.name}: {e}")

        meta_data = data.get("super_meta", {})
        metadata_size = int(meta_data.get("metadata_size", 65536))
        metadata_slots = int(meta_data.get("metadata_slots", 2))

        super_def = SuperDef(metadata_size=metadata_size, metadata_slots=metadata_slots)

        block_devices = data.get("block_devices", [])
        if not block_devices:
            if "super_size" in data:
                block_devices.append({
                    "name": "super",
                    "size": data["super_size"],
                    "alignment": data.get("alignment", 1048576),
                    "block_size": data.get("block_size", 4096)
                })
            else:
                raise InvalidSuperDefError("Missing 'block_devices' or fallback 'super_size' configuration.")

        for dev in block_devices:
            super_def.block_devices.append(BlockDevice(
                name=dev.get("name", "super"),
                size=int(dev["size"]),
                alignment=int(dev.get("alignment", 1048576)),
                block_size=int(dev.get("block_size", 4096))
            ))

        groups = data.get("groups", [])
        for group in groups:
            super_def.groups.append(PartitionGroup(
                name=group["name"],
                maximum_size=int(group.get("maximum_size", 0))
            ))

        partitions = data.get("partitions", [])
        for part in partitions:
            if "name" not in part:
                raise InvalidSuperDefError("Partition entry missing name property.")
                
            super_def.partitions.append(Partition(
                name=part["name"],
                group_name=part.get("group_name", "default"),
                path=part.get("path"),
                size=int(part.get("size", 0)),
                attributes=part.get("attributes", "readonly"),
                is_empty=part.get("is_empty", False)
            ))

        return super_def

    @staticmethod
    def save(super_def: SuperDef, output_path: Path) -> None:
        """Serializes SuperDef models back to clean super_def.json configurations."""
        data = {
            "super_meta": {
                "metadata_size": super_def.metadata_size,
                "metadata_slots": super_def.metadata_slots
            },
            "block_devices": [
                {
                    "name": dev.name,
                    "size": dev.size,
                    "alignment": dev.alignment,
                    "block_size": dev.block_size
                } for dev in super_def.block_devices
            ],
            "groups": [
                {
                    "name": grp.name,
                    "maximum_size": grp.maximum_size
                } for grp in super_def.groups
            ],
            "partitions": [
                {
                    "name": part.name,
                    "group_name": part.group_name,
                    "path": part.path if part.path else "",
                    "size": part.size,
                    "attributes": part.attributes,
                    "is_empty": part.is_empty
                } for part in super_def.partitions
            ]
        }
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)


# ==============================================================================
# 5. Layout Validator
# ==============================================================================

class SuperDefValidator:
    def __init__(self, firmware_dir: Path, skip_empty: bool = False, create_empty: bool = False):
        self.firmware_dir = firmware_dir
        self.skip_empty = skip_empty
        self.create_empty = create_empty

    def validate(self, super_def: SuperDef) -> Dict[str, int]:
        """Validates partition images against sizes configured in the JSON template."""
        logger.info("[INFO] Validating image paths and sizes from JSON layout...")
        
        if not super_def.block_devices:
            raise InvalidSuperDefError("No block devices defined.")
        
        seen_names: Set[str] = set()
        seen_paths: Set[str] = set()
        final_sizes: Dict[str, int] = {}
        validated_partitions: List[Partition] = []

        group_names = {grp.name for grp in super_def.groups}
        if "default" not in group_names:
            group_names.add("default")

        for part in super_def.partitions:
            if part.name in seen_names:
                raise InvalidSuperDefError(f"Duplicate partition entry: '{part.name}'")
            seen_names.add(part.name)

            if part.group_name not in group_names:
                raise InvalidSuperDefError(f"Partition '{part.name}' references undefined group: '{part.group_name}'")

            img_path: Path | None = None
            
            if part.path:
                img_path = self.firmware_dir / part.path
                if not img_path.exists():
                    img_path = Path(part.path)

                if img_path.exists() and img_path.is_file():
                    resolved_key = str(img_path.resolve())
                    if resolved_key in seen_paths:
                        raise InvalidSuperDefError(f"Duplicate image mapping path: {part.path}")
                    seen_paths.add(resolved_key)
                else:
                    img_path = None

            if not img_path:
                if self.skip_empty:
                    logger.info(f"[INFO] Skipping empty segment: {part.name}")
                    continue
                
                if part.size <= 0:
                    if self.create_empty:
                        logger.warning(f"Creating empty partition entry: {part.name}")
                        part.is_empty = True
                    else:
                        raise MissingImageError(
                            f"Missing valid image file or layout size for: '{part.name}'. "
                            "Add an image or specify --create-empty-slots."
                        )

            resolved_size = part.size
            if resolved_size <= 0 and img_path:
                resolved_size = get_unsparsed_size(img_path)
                logger.warning(f"[WARNING] Partition '{part.name}' size is 0 in JSON. Defaulting to file size: {resolved_size} bytes")

            if img_path and not part.is_empty:
                actual_size = get_unsparsed_size(img_path)
                if actual_size > resolved_size:
                    raise SuperBuilderError(
                        f"Image size for partition '{part.name}' ({actual_size} bytes) "
                        f"exceeds its allocated slot capacity of {resolved_size} bytes."
                    )
                else:
                    logger.debug(f"Partition '{part.name}' fits allocation limits ({actual_size} <= {resolved_size})")

            block_size = super_def.block_devices[0].block_size
            if resolved_size % block_size != 0:
                old_size = resolved_size
                resolved_size = ((resolved_size + block_size - 1) // block_size) * block_size
                logger.warning(f"[WARNING] Aligning '{part.name}' size from {old_size} to sector boundary {resolved_size}")

            final_sizes[part.name] = resolved_size
            validated_partitions.append(part)
            logger.info(f"[OK] {part.name} size verified: {resolved_size} bytes")

        super_def.partitions = validated_partitions

        # Group checking
        group_totals: Dict[str, int] = {}
        for part in super_def.partitions:
            group_totals[part.group_name] = group_totals.get(part.group_name, 0) + final_sizes[part.name]

        for grp in super_def.groups:
            tot = group_totals.get(grp.name, 0)
            if grp.maximum_size > 0 and tot > grp.maximum_size:
                raise GroupSizeExceededError(
                    f"Partition sizes in group '{grp.name}' total {tot} bytes. "
                    f"This exceeds maximum group capacity limit '{grp.maximum_size}' by {tot - grp.maximum_size} bytes."
                )

        # Super capacity layout bounds checking
        total_parts_size = sum(final_sizes.values())
        super_capacity = super_def.block_devices[0].size
        available_space = super_capacity - (super_def.metadata_size * super_def.metadata_slots)
        if total_parts_size > available_space:
            raise GroupSizeExceededError(
                f"Required allocation ({total_parts_size} bytes) exceeds physical "
                f"super device size parameters ({available_space} bytes usable space)."
            )

        logger.info("[OK] Layout validation checks successfully passed.")
        return final_sizes


# ==============================================================================
# 6. lpmake Parameter Builder
# ==============================================================================

class LpmakeCommandGenerator:
    @staticmethod
    def generate(
        super_def: SuperDef,
        final_sizes: Dict[str, int],
        firmware_dir: Path,
        output_file: Path,
        sparse: bool = False
    ) -> List[str]:
        """Translates schema structures to a complete lpmake CLI command list."""
        cmd = ["lpmake"]
        cmd.extend(["--metadata-size", str(super_def.metadata_size)])
        cmd.extend(["--metadata-slots", str(super_def.metadata_slots)])
        
        dev = super_def.block_devices[0]
        cmd.extend(["--device", f"{dev.name}:{dev.size}"])
        
        for group in super_def.groups:
            if group.name == "default":
                continue
            cmd.extend(["--group", f"{group.name}:{group.maximum_size}"])

        for part in super_def.partitions:
            part_size = final_sizes.get(part.name, 0)
            cmd.extend(["--partition", f"{part.name}:{part.attributes}:{part_size}:{part.group_name}"])
            
            if part.path and not part.is_empty:
                img_path = firmware_dir / part.path
                if not img_path.exists():
                    img_path = Path(part.path)
                cmd.extend(["--image", f"{part.name}={img_path}"])

        if sparse:
            cmd.append("-F")
            
        cmd.extend(["-o", str(output_file)])
        return cmd

    @staticmethod
    def write_build_script(cmd: List[str], script_path: Path) -> None:
        """Generates build.sh output executable containing the raw build command."""
        escaped_cmd = []
        for arg in cmd:
            if any(char in arg for char in (' ', '"', "'", '=', ':')):
                escaped_cmd.append(f'"{arg}"')
            else:
                escaped_cmd.append(arg)

        with open(script_path, 'w', encoding='utf-8') as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("# Generated dynamically by MTK-SuperBuilder.\n")
            f.write("set -xe\n\n")
            f.write(" \\\n  ".join(escaped_cmd) + "\n")
        
        try:
            script_path.chmod(0o755)
        except Exception:
            pass


# ==============================================================================
# 7. Post-Build Verifier (lpdump Driver)
# ==============================================================================

class SuperVerifier:
    def __init__(self, super_img: Path):
        self.super_img = super_img

    def verify(self, super_def: SuperDef, expected_sizes: Dict[str, int]) -> bool:
        """Runs lpdump on the compiled super.img to verify sizes and layouts match JSON exactly."""
        logger.info("[INFO] Initiating optional verification checks via lpdump...")
        lpdump_bin = check_binary("lpdump")
        ret, stdout, stderr = run_command([str(lpdump_bin), str(self.super_img)])
        if ret != 0:
            logger.error(f"lpdump verification failed to run. stderr: {stderr}")
            return False

        failures = 0
        for part in super_def.partitions:
            expected_sz = expected_sizes.get(part.name, 0)
            pattern = rf"Name:\s+{part.name}\s+.*?Group:\s+{part.group_name}\s+.*?Length:\s+(\d+)\s+bytes"
            match = re.search(pattern, stdout, re.DOTALL | re.IGNORECASE)

            if not match:
                if expected_sz == 0:
                    logger.info(f"[OK] Verified empty slot built: '{part.name}'")
                    continue
                logger.error(f"[FAIL] Partition '{part.name}' is missing from built metadata table.")
                failures += 1
                continue

            built_sz = int(match.group(1))
            if built_sz != expected_sz:
                logger.error(f"[FAIL] Size mismatch on '{part.name}': JSON configured size is {expected_sz} bytes but lpdump reported {built_sz} bytes.")
                failures += 1
            else:
                logger.info(f"[OK] Verified compiled metadata matches: '{part.name}' ({built_sz} bytes)")

        if failures > 0:
            logger.error(f"[FAIL] Post-build lpdump checks failed. Built image has {failures} discrepancies.")
            return False

        logger.info("[SUCCESS] Verification checks completed successfully.")
        return True


# ==============================================================================
# 8. Super Decompiler / Importer
# ==============================================================================

class SuperImporter:
    def __init__(self, super_img: Path, output_dir: Path):
        self.super_img = super_img
        self.output_dir = output_dir
        self.images_dir = output_dir / "IMAGES"

    def import_project(self) -> bool:
        """Deconstructs a super.img binary back into its base elements and JSON configuration."""
        logger.info(f"[INFO] Importing and unpacking project: {self.super_img.name}")
        
        lpdump_bin = check_binary("lpdump")
        lpunpack_bin = check_binary("lpunpack")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

        logger.info("[INFO] Reading metadata slots and groups structure via lpdump analysis...")
        ret, stdout, stderr = run_command([str(lpdump_bin), str(self.super_img)])
        if ret != 0:
            logger.error(f"Failed to read super metadata using lpdump: {stderr}")
            return False

        try:
            super_def = self._parse_lpdump_output(stdout)
        except Exception as e:
            logger.error(f"Failed parsing lpdump output structure: {e}")
            logger.debug(f"Raw analysis table:\n{stdout}")
            return False

        logger.info("[BUILD] Extracting partitions using lpunpack...")
        ret_unp, _, stderr_unp = run_command([str(lpunpack_bin), str(self.super_img), str(self.images_dir)])
        if ret_unp != 0:
            logger.error(f"Failed to unpack images: {stderr_unp}")
            return False

        partitions: List[Partition] = []
        for file in self.images_dir.iterdir():
            if file.is_file() and file.suffix == ".img":
                part_name = file.stem
                part_size = file.stat().st_size
                
                original_part = super_def.find_partition(part_name)
                group_name = original_part.group_name if original_part else "default"
                attributes = original_part.attributes if original_part else "readonly"

                partitions.append(Partition(
                    name=part_name,
                    group_name=group_name,
                    path=f"IMAGES/{file.name}",
                    size=part_size,
                    attributes=attributes,
                    is_empty=False
                ))
                logger.info(f"[OK] Discovered unpacked partition '{part_name}' size: {part_size} bytes (Mapped to Group: {group_name})")

        super_def.partitions = partitions
        super_def.block_devices[0].size = self.super_img.stat().st_size

        config_out = self.output_dir / "super_def.json"
        SuperDefParser.save(super_def, config_out)
        logger.info(f"[OK] Blueprint configuration generated: {config_out.name}")
        logger.info(f"[SUCCESS] Rebuild workspace successfully initialized at: {self.output_dir}")
        return True

    def _parse_lpdump_output(self, raw_dump: str) -> SuperDef:
        metadata_size = 65536
        metadata_slots = 2
        block_devices: List[BlockDevice] = []
        groups: List[PartitionGroup] = []
        partitions: List[Partition] = []

        state = "GLOBAL"
        lines = raw_dump.splitlines()
        
        current_partition: Dict[str, Any] = {}
        current_device: Dict[str, Any] = {}
        current_group: Dict[str, Any] = {}

        meta_size_match = re.search(r"Metadata size:\s+(\d+)\s+bytes", raw_dump)
        if meta_size_match:
            metadata_size = int(meta_size_match.group(1))

        meta_slots_match = re.search(r"Metadata slot count:\s+(\d+)", raw_dump)
        if meta_slots_match:
            metadata_slots = int(meta_slots_match.group(1))

        for line in lines:
            line_strip = line.strip()
            if not line_strip:
                continue

            if "Block device table:" in line_strip:
                state = "BLOCK_DEVICE"
                continue
            elif "Group table:" in line_strip:
                state = "GROUP"
                continue
            elif "Partition table:" in line_strip:
                state = "PARTITION"
                continue
            elif line_strip.startswith("-----") or line_strip.startswith("====="):
                continue

            if state == "BLOCK_DEVICE":
                name_match = re.match(r"(?:Partition\s+)?Name:\s+(\w+)", line_strip, re.IGNORECASE)
                size_match = re.match(r"Size:\s+(\d+)\s+bytes", line_strip, re.IGNORECASE)
                
                if name_match:
                    if current_device:
                        block_devices.append(BlockDevice(**current_device))
                    current_device = {"name": name_match.group(1), "size": 0}
                elif size_match and current_device:
                    current_device["size"] = int(size_match.group(1))

            elif state == "GROUP":
                name_match = re.match(r"Name:\s+(\w+)", line_strip, re.IGNORECASE)
                max_size_match = re.match(r"Maximum\s+size:\s+(\d+)\s+bytes", line_strip, re.IGNORECASE)
                
                if name_match:
                    if current_group:
                        groups.append(PartitionGroup(**current_group))
                    current_group = {"name": name_match.group(1), "maximum_size": 0}
                elif max_size_match and current_group:
                    current_group["maximum_size"] = int(max_size_match.group(1))

            elif state == "PARTITION":
                name_match = re.match(r"Name:\s+([\w_+-]+)", line_strip, re.IGNORECASE)
                group_match = re.match(r"Group:\s+([\w_+-]+)", line_strip, re.IGNORECASE)
                attrs_match = re.match(r"Attributes:\s+(\w+)", line_strip, re.IGNORECASE)
                size_match = re.match(r"(?:Length|Size):\s+(\d+)\s+bytes", line_strip, re.IGNORECASE)

                if name_match:
                    if current_partition:
                        partitions.append(self._make_partition_obj(current_partition))
                    current_partition = {"name": name_match.group(1)}
                elif group_match and current_partition:
                    current_partition["group_name"] = group_match.group(1)
                elif attrs_match and current_partition:
                    current_partition["attributes"] = attrs_match.group(1)
                elif size_match and current_partition:
                    current_partition["size"] = int(size_match.group(1))

        if current_device:
            block_devices.append(BlockDevice(**current_device))
        if current_group:
            groups.append(PartitionGroup(**current_group))
        if current_partition:
            partitions.append(self._make_partition_obj(current_partition))

        if not block_devices:
            block_devices.append(BlockDevice(name="super", size=0))

        return SuperDef(
            metadata_size=metadata_size,
            metadata_slots=metadata_slots,
            block_devices=block_devices,
            groups=groups,
            partitions=partitions
        )

    def _make_partition_obj(self, p_dict: Dict[str, Any]) -> Partition:
        name = p_dict.get("name", "")
        return Partition(
            name=name,
            group_name=p_dict.get("group_name", "default"),
            path=str(Path("IMAGES") / f"{name}.img"),
            size=p_dict.get("size", 0),
            attributes=p_dict.get("attributes", "readonly"),
            is_empty=False
        )


# ==============================================================================
# 9. Core Builder Orchestrator
# ==============================================================================

class SuperBuilder:
    def __init__(
        self,
        firmware_dir: Path,
        skip_empty: bool = False,
        create_empty: bool = False,
        unsparse_images: bool = False,
        dry_run: bool = False,
        print_cmd: bool = False,
        sparse_out: bool = False,
        verify: bool = False
    ):
        self.firmware_dir = firmware_dir
        self.skip_empty = skip_empty
        self.create_empty = create_empty
        self.unsparse_images = unsparse_images
        self.dry_run = dry_run
        self.print_cmd = print_cmd
        self.sparse_out = sparse_out
        self.verify = verify

    def build(self, super_def: SuperDef, output_img: Path) -> bool:
        """Compiles the dynamic partitions schema into the final super image file."""
        if not self.dry_run:
            check_binary("lpmake")

        validator = SuperDefValidator(
            self.firmware_dir,
            self.skip_empty,
            self.create_empty
        )
        final_sizes = validator.validate(super_def)

        temp_raw_images: Dict[str, Path] = {}
        try:
            if not self.dry_run and self.unsparse_images:
                for part in super_def.partitions:
                    if part.path and not part.is_empty:
                        img_path = self.firmware_dir / part.path
                        if not img_path.exists():
                            img_path = Path(part.path)

                        if img_path.exists() and is_sparse_image(img_path):
                            raw_temp = img_path.with_suffix(".img.raw")
                            convert_sparse_to_raw(img_path, raw_temp)
                            temp_raw_images[part.name] = raw_temp
                            part.path = str(raw_temp.relative_to(self.firmware_dir) if raw_temp.is_relative_to(self.firmware_dir) else raw_temp)

            cmd = LpmakeCommandGenerator.generate(
                super_def,
                final_sizes,
                self.firmware_dir,
                output_img,
                self.sparse_out
            )

            if self.print_cmd:
                logger.info("[BUILD] Generated lpmake execution sequence:")
                print("\n" + " ".join(cmd) + "\n")

            script_path = self.firmware_dir / "build.sh"
            LpmakeCommandGenerator.write_build_script(cmd, script_path)
            logger.info(f"[OK] Shell launch script saved: {script_path.name}")

            if self.dry_run:
                logger.info("[SUCCESS] Dry-run testing passed cleanly. Build sequence finished.")
                return True

            logger.info(f"[BUILD] Running lpmake to compile output image...")
            ret, _, err = run_command(cmd)
            if ret != 0:
                logger.error(f"lpmake compiler threw an error: {err}")
                return False

            logger.info(f"[SUCCESS] Compiled image built: {output_img.name}")
            
            if self.verify:
                verifier = SuperVerifier(output_img)
                if not verifier.verify(super_def, final_sizes):
                    return False

            self._write_report(output_img, super_def, True)
            return True

        finally:
            for raw_img in temp_raw_images.values():
                if raw_img.exists():
                    logger.debug(f"Deleting raw conversion mapping file: {raw_img.name}")
                    try:
                        raw_img.unlink()
                    except Exception as e:
                        logger.warning(f"Failed deleting temporary raw map output: {e}")

    def _write_report(self, output_file: Path, super_def: SuperDef, verification: bool) -> None:
        report_path = self.firmware_dir / "build_report.json"
        report_data = {
            "status": "success" if verification else "verification_failed",
            "output_file": str(output_file.name),
            "super_size": super_def.block_devices[0].size,
            "groups": len(super_def.groups),
            "partitions": len(super_def.partitions)
        }
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(report_data, f, indent=4)
            logger.info(f"[OK] Summary reports updated: {report_path.name}")
        except Exception as e:
            logger.warning(f"Error compiling output telemetry metadata reports: {e}")


# ==============================================================================
# 10. CLI Entrypoint
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MTK-SuperBuilder: Standalone Android Dynamic Partition Compiler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Commands mapping options:
  python3 superbuilder.py info ./firmware
  python3 superbuilder.py validate ./firmware --create-empty-slots
  python3 superbuilder.py build ./firmware --print-cmd --verify
  python3 superbuilder.py import ./super.img -o ./firmware_workspace
"""
    )
    
    parser.add_argument("-v", "--verbose", action="store_true", help="Print debug details to console.")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Runtime action command mapping.")

    # Action 'info'
    info_parser = subparsers.add_parser("info", help="Inspect and display partition structures.")
    info_parser.add_argument("firmware_dir", type=str, help="Firmware project directory containing super_def.json")

    # Action 'validate'
    validate_parser = subparsers.add_parser("validate", help="Validate layout and constraints settings.")
    validate_parser.add_argument("firmware_dir", type=str, help="Firmware project directory containing super_def.json")
    validate_parser.add_argument("--skip-empty-slots", action="store_true", help="Ignore alignment constraints on unassigned spaces.")
    validate_parser.add_argument("--create-empty-slots", action="store_true", help="Format auto-generated placeholders for empty sectors.")

    # Action 'generate'
    generate_parser = subparsers.add_parser("generate", help="Generate building execution systems command strings.")
    generate_parser.add_argument("firmware_dir", type=str, help="Firmware project directory containing super_def.json")
    generate_parser.add_argument("-o", "--output", type=str, default="super.img", help="Output filename.")
    generate_parser.add_argument("--sparse", action="store_true", help="Generate building steps in Android sparse format output.")

    # Action 'build'
    build_parser = subparsers.add_parser("build", help="Pack structures to output file representation.")
    build_parser.add_argument("firmware_dir", type=str, help="Firmware project directory containing super_def.json")
    build_parser.add_argument("-o", "--output", type=str, default="super.img", help="Output image file target path.")
    build_parser.add_argument("--skip-empty-slots", action="store_true", help="Do not generate layout settings for empty parts.")
    build_parser.add_argument("--create-empty-slots", action="store_true", help="Generate size 0 placeholder slots where images are missing.")
    build_parser.add_argument("--unsparse", action="store_true", help="Convert detected sparse images to raw before compiling (requires simg2img).")
    build_parser.add_argument("--dry-run", action="store_true", help="Run checking structures without executing lpmake builds.")
    build_parser.add_argument("--print-cmd", action="store_true", help="Display compiler execution pipeline steps to output console.")
    build_parser.add_argument("--sparse", action="store_true", help="Output generated images in Android sparse formatting.")
    build_parser.add_argument("--verify", action="store_true", help="Verify final built image structural layout against configuration using lpdump.")

    # Action 'import'
    import_parser = subparsers.add_parser("import", help="Deconstruct and decompile physical dynamic system images.")
    import_parser.add_argument("super_img", type=str, help="Path to raw super.img file.")
    import_parser.add_argument("-o", "--output", type=str, default="./imported_project", help="Workspace folder output directory.")

    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        if args.command == "info":
            firmware_dir = Path(args.firmware_dir)
            super_def = SuperDefParser.parse(firmware_dir)
            
            print("\n" + "="*60)
            print("        MTK-SUPERBUILDER - SUPER LAYOUT PROFILE INFO")
            print("="*60)
            print(f"Metadata Size   : {super_def.metadata_size} bytes")
            print(f"Metadata Slots  : {super_def.metadata_slots}")
            
            print("\n--- Block Devices ---")
            for dev in super_def.block_devices:
                print(f"  Name: {dev.name:<15} Size: {dev.size:<15} Aligned: {dev.alignment:<10} Block: {dev.block_size}")
                
            print("\n--- Dynamic Partition Groups ---")
            for group in super_def.groups:
                limit_str = f"{group.maximum_size} bytes" if group.maximum_size > 0 else "Dynamic (Unlimited)"
                print(f"  Group: {group.name:<15} Max Size Limit: {limit_str}")
                
            print("\n--- Partition Mapping Lists ---")
            for part in super_def.partitions:
                empty_tag = " [EMPTY SPACE]" if part.is_empty else ""
                print(f"  Name: {part.name:<20} Group: {part.group_name:<15} Size: {part.size:<12} Attributes: {part.attributes}{empty_tag}")
            print("="*60 + "\n")

        elif args.command == "validate":
            firmware_dir = Path(args.firmware_dir)
            super_def = SuperDefParser.parse(firmware_dir)
            
            validator = SuperDefValidator(
                firmware_dir,
                skip_empty=args.skip_empty_slots,
                create_empty=args.create_empty_slots
            )
            validator.validate(super_def)

        elif args.command == "generate":
            firmware_dir = Path(args.firmware_dir)
            super_def = SuperDefParser.parse(firmware_dir)
            
            validator = SuperDefValidator(firmware_dir)
            sizes = validator.validate(super_def)
            
            output_file = Path(args.output)
            cmd = LpmakeCommandGenerator.generate(super_def, sizes, firmware_dir, output_file, args.sparse)
            
            print("\n--- GENERATED SYSTEM ARGS COMMAND SEQUENCE ---")
            print(" \\\n  ".join(cmd))
            print("--------------------------------------------\n")

        elif args.command == "build":
            firmware_dir = Path(args.firmware_dir)
            super_def = SuperDefParser.parse(firmware_dir)
            
            output_file = Path(args.output)
            if not output_file.is_absolute() and not (firmware_dir / args.output).parent.exists():
                output_file = firmware_dir / args.output

            builder = SuperBuilder(
                firmware_dir=firmware_dir,
                skip_empty=args.skip_empty_slots,
                create_empty=args.create_empty_slots,
                unsparse_images=args.unsparse,
                dry_run=args.dry_run,
                print_cmd=args.print_cmd,
                sparse_out=args.sparse,
                verify=args.verify
            )
            
            if not builder.build(super_def, output_file):
                sys.exit(1)

        elif args.command == "import":
            super_img = Path(args.super_img)
            output_dir = Path(args.output)
            
            importer = SuperImporter(super_img, output_dir)
            if not importer.import_project():
                sys.exit(1)

    except SuperBuilderError as e:
        logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        logger.exception(f"Unhandled engineering runtime error encountered: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
