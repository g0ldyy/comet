import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DIST_DIR = Path("dist")


def collect_addons(dist: Path):
    addons = []
    for xml_path in sorted(dist.glob("*/addon.xml")):
        element = ET.parse(xml_path).getroot()
        if element.tag != "addon":
            print(
                f"Skipping {xml_path}: invalid root tag '{element.tag}'",
                file=sys.stderr,
            )
            continue
        addons.append(element)
    return addons


def main():
    addons = collect_addons(DIST_DIR)
    if not addons:
        print("No addons found in dist/", file=sys.stderr)
        sys.exit(1)

    root = ET.Element("addons")
    root.extend(addons)

    xml_path = DIST_DIR / "addons.xml"
    ET.ElementTree(root).write(xml_path, encoding="utf-8", xml_declaration=True)

    md5 = hashlib.md5(xml_path.read_bytes()).hexdigest()
    (DIST_DIR / "addons.xml.md5").write_text(md5)

    print(f"Generated {xml_path} ({len(addons)} addons) + addons.xml.md5")


if __name__ == "__main__":
    main()
