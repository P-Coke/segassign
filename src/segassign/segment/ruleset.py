"""Narrow eCognition ruleset adaptation kept behind the vendor boundary."""

from __future__ import annotations

import copy
import html
import xml.etree.ElementTree as ET
from pathlib import Path


def _property(root, identifier):
    for node in root.findall(".//PropDscr"):
        descriptor = node.find(".//PropDscrId")
        if descriptor is not None and descriptor.get("InstID") == identifier:
            return node
    return None


def _required(root, expression: str, label: str):
    node = root.find(expression)
    if node is None:
        raise ValueError(f"ruleset is missing {label}")
    return node


def _patch_export(root, directory: Path) -> None:
    target = str(directory)
    for node in root.findall(".//DValue[@name='strExportPath']"):
        node.set("value", target)
    for node in root.findall(".//DValue[@name='strExportItemInfo']"):
        try:
            item = ET.fromstring(html.unescape(node.get("value", "")))
        except ET.ParseError:
            continue
        item.set("ExportPath", target)
        node.set("value", ET.tostring(item, encoding="unicode"))
    for node in root.findall(".//ExportedItems/item[@name='ObjectShapes']"):
        node.set("path", target)


def patch(
    template: Path, destination: Path, export_directory: Path, mode: str, reference: Path, class_name: str
) -> None:
    tree = ET.parse(template)
    root = tree.getroot()
    if mode == "invalid_classify":
        reference_root = ET.parse(reference).getroot()
        properties = _required(root, ".//ClssHrchy/PropTree/AllProps", "class properties")
        for identifier in ("Mean Layer 1", "Mean Layer 5"):
            if _property(root, identifier) is None:
                source = _property(reference_root, identifier)
                if source is None:
                    raise ValueError(f"reference ruleset is missing {identifier}")
                properties.append(copy.deepcopy(source))
        classes = _required(root, ".//ClssHrchy/AllClss", "classes")
        if not any(node.get("name") == class_name for node in classes.findall("Clss")):
            source = next(
                (
                    node
                    for node in reference_root.findall(".//ClssHrchy/AllClss/Clss")
                    if node.get("name") == class_name
                ),
                None,
            )
            if source is None:
                raise ValueError(f"reference ruleset is missing class {class_name!r}")
            classes.append(copy.deepcopy(source))
        for expression in (".//ClssHrchy/AllSubClss", ".//ClssHrchy/AllTerm"):
            target = root.find(expression)
            source = reference_root.find(expression)
            if target is not None and source is not None and not len(target):
                for child in source:
                    target.append(copy.deepcopy(child))
        process_list = _required(root, ".//ProcessList", "process list")
        invalid_process = next(
            (
                node
                for node in reference_root.findall(".//ProcessList/ProcBase")
                if "Mean Layer 5 = 0" in node.get("Name", "")
            ),
            None,
        )
        if invalid_process is None:
            raise ValueError("reference ruleset is missing the invalid-class process")
        if not any("Mean Layer 5 = 0" in node.get("Name", "") for node in process_list.findall("ProcBase")):
            export_index = next(
                (
                    index
                    for index, node in enumerate(process_list)
                    if "export object shapes" in node.get("Name", "").lower()
                ),
                None,
            )
            if export_index is None:
                raise ValueError("ruleset is missing the ObjectShapes export process")
            process_list.insert(export_index, copy.deepcopy(invalid_process))
    elif mode != "plain":
        raise ValueError(f"unsupported ruleset mode: {mode}")
    _patch_export(root, export_directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="UTF-8", xml_declaration=True)


def retarget(source: Path, destination: Path, export_directory: Path) -> None:
    tree = ET.parse(source)
    _patch_export(tree.getroot(), export_directory)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tree.write(destination, encoding="UTF-8", xml_declaration=True)
