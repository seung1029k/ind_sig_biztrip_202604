from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image


PDFS = [
    Path("global_business_overview_mar_2026_v3.pdf"),
    Path("global_hr_data_book_20260331_v1.pdf"),
]
OUTPUT_DIR = Path("extracted_people_photos")
MANIFEST_PATH = OUTPUT_DIR / "manifest.csv"

TITLE_LINES = {
    "대표",
    "이사",
    "소장",
    "상무",
    "전무",
    "부사장",
    "수석매니저",
    "실장",
    "대표이사",
    "매니저",
    "ceo",
    "cfo",
    "coo",
    "cto",
    "cro",
    "cco",
    "cbo",
    "head",
    "director",
    "associate",
    "manager",
    "vice president",
    "vp",
    "svp",
    "avp",
    "ed",
    "md",
}
NEXT_HEADER_WORDS = {"education", "birth", "join", "contact", "experience"}


@dataclass
class Candidate:
    name: str
    source_pdf: str
    page: int
    image_bytes: bytes
    width: int
    height: int


def sanitize_filename(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*]', "", name)
    name = name.rstrip(". ")
    return name or "unknown"


def normalize_text(text: str) -> str:
    text = text.replace("\uf09e", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_parenthetical_tail(text: str) -> str:
    text = normalize_text(text)
    if text.startswith("("):
        return ""
    return text.split("(", 1)[0].strip()


def has_real_name_chars(text: str) -> bool:
    compact = re.sub(r"[^A-Za-z가-힣.\- ]", "", text)
    compact = compact.replace(" ", "")
    return len(compact) >= 2


def clean_candidate_name(name: str | None) -> str | None:
    if not name:
        return None
    name = normalize_text(name)
    name = re.sub(r"^Name\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+(학사|석사|mba)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^(Univ\.?|University)\s+", "", name, flags=re.IGNORECASE)
    name = name.strip(" -,:;")
    if not has_real_name_chars(name):
        return None
    return name


def name_quality(name: str) -> int:
    score = len(name)
    lowered = name.lower()
    if lowered.startswith("name "):
        score -= 50
    if re.search(r"\b(univ|university|college|school)\b", lowered):
        score -= 20
    if re.search(r"(학사|석사)", name):
        score -= 20
    if re.search(r"[A-Za-z]", name):
        score += 5
    if re.search(r"[가-힣]", name):
        score += 4
    if " " in name:
        score += 3
    if len(name) <= 4 and " " not in name and not re.search(r"[A-Za-z]\.", name):
        score -= 6
    return score


def is_title_line(text: str) -> bool:
    normalized = normalize_text(text)
    if normalized.startswith("(") and normalized.endswith(")"):
        return True
    bare = normalized.strip("()").strip()
    if not bare:
        return True
    if bare.lower() in TITLE_LINES:
        return True
    if bare in TITLE_LINES:
        return True
    if re.fullmatch(r"[A-Z]{2,4}", bare):
        return True
    if bare.startswith("(") and bare.endswith(")"):
        return True
    if re.search(
        r"\b(Director|Manager|Associate|Head|Chief|President|Representative|MD|ED|VP|SVP|AVP)\b",
        bare,
        re.IGNORECASE,
    ):
        return True
    if re.search(r"(대표|이사|소장|상무|전무|부사장|매니저|실장)", bare):
        return True
    return False


def cluster_word_lines(words: list[tuple], y_tolerance: float = 3.0) -> list[list[tuple]]:
    lines: list[list[tuple]] = []
    sorted_words = sorted(words, key=lambda w: (w[1], w[0]))
    for word in sorted_words:
        x0, y0, x1, y1, text, *_ = word
        if not lines:
            lines.append([word])
            continue
        last_line = lines[-1]
        last_y = sum(item[1] for item in last_line) / len(last_line)
        if abs(y0 - last_y) <= y_tolerance:
            last_line.append(word)
        else:
            lines.append([word])
    return lines


def leftmost_cluster_text(line_words: list[tuple], gap_tolerance: float = 18.0) -> str:
    sorted_words = sorted(line_words, key=lambda w: w[0])
    clusters: list[list[tuple]] = []
    for word in sorted_words:
        if not clusters:
            clusters.append([word])
            continue
        prev = clusters[-1][-1]
        if word[0] - prev[2] <= gap_tolerance:
            clusters[-1].append(word)
        else:
            clusters.append([word])
    chosen = clusters[0]
    return normalize_text(" ".join(item[4] for item in chosen))


def parse_name(words: list[tuple]) -> str | None:
    lines = []
    for line_words in cluster_word_lines(words):
        text = leftmost_cluster_text(line_words)
        if text:
            lines.append(text)

    if not lines:
        return None

    name_parts: list[str] = []
    for line in lines:
        if is_title_line(line):
            break
        clean_line = strip_parenthetical_tail(line)
        if not clean_line:
            break
        if not has_real_name_chars(clean_line):
            continue
        name_parts.append(clean_line)
        if len(name_parts) >= 3:
            break

    if not name_parts:
        return None

    if len(name_parts) == 1:
        return name_parts[0]

    return normalize_text(" ".join(name_parts))


def find_name_column(words: list[tuple], image_bbox: tuple[float, float, float, float]) -> tuple[float, float] | None:
    image_y0 = image_bbox[1]
    name_headers = [w for w in words if str(w[4]).lower() == "name" and w[1] < image_y0]
    if not name_headers:
        return None

    name_header = max(name_headers, key=lambda w: w[1])
    header_row = [w for w in words if abs(w[1] - name_header[1]) <= 3.0]
    next_headers = [
        w
        for w in header_row
        if w[0] > name_header[0] + 5 and str(w[4]).lower() in NEXT_HEADER_WORDS
    ]
    next_x0 = min((w[0] for w in next_headers), default=name_header[2] + 120)
    return name_header[0] - 4, next_x0 - 8


def row_name_words(words: list[tuple], image_bbox: tuple[float, float, float, float]) -> list[tuple]:
    column = find_name_column(words, image_bbox)
    if not column:
        return []

    col_x0, col_x1 = column
    x0, y0, x1, y1 = image_bbox
    margin = 10
    selected = []
    for word in words:
        wx0, wy0, wx1, wy1, text, *_ = word
        center_x = (wx0 + wx1) / 2
        overlap = max(0, min(y1 + margin, wy1) - max(y0 - margin, wy0))
        if not (col_x0 <= center_x <= col_x1):
            continue
        if overlap <= 0:
            continue
        selected.append(word)
    return selected


def candidate_images(page: fitz.Page) -> list[Candidate]:
    page_dict = page.get_text("dict")
    words = page.get_text("words")
    results: list[Candidate] = []

    for block in page_dict["blocks"]:
        if block.get("type") != 1:
            continue
        if block.get("width", 0) < 80 or block.get("height", 0) < 80:
            continue

        bbox = tuple(block["bbox"])
        display_w = bbox[2] - bbox[0]
        display_h = bbox[3] - bbox[1]
        if not (35 <= display_w <= 140 and 45 <= display_h <= 170):
            continue

        name_words = row_name_words(words, bbox)
        name = clean_candidate_name(parse_name(name_words))
        if not name:
            continue

        results.append(
            Candidate(
                name=name,
                source_pdf="",
                page=page.number + 1,
                image_bytes=block["image"],
                width=block["width"],
                height=block["height"],
            )
        )

    return results


def image_to_png_bytes(raw: bytes) -> tuple[bytes, int]:
    with Image.open(io.BytesIO(raw)) as image:
        image = image.convert("RGB")
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue(), image.width * image.height


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)

    best_by_digest: dict[str, dict] = {}

    for pdf_path in PDFS:
        doc = fitz.open(pdf_path)
        for page in doc:
            for found in candidate_images(page):
                found.source_pdf = pdf_path.name
                png_bytes, area = image_to_png_bytes(found.image_bytes)
                digest = hashlib.sha1(png_bytes).hexdigest()

                item = {
                    "name": found.name,
                    "source_pdf": found.source_pdf,
                    "page": found.page,
                    "png_bytes": png_bytes,
                    "area": area,
                    "digest": digest,
                    "quality": name_quality(found.name),
                }
                existing = best_by_digest.get(digest)
                if not existing:
                    best_by_digest[digest] = item
                    continue
                if item["quality"] > existing["quality"] or (
                    item["quality"] == existing["quality"] and item["area"] > existing["area"]
                ):
                    best_by_digest[digest] = item

    best_by_name: dict[str, dict] = {}
    for item in best_by_digest.values():
        existing = best_by_name.get(item["name"])
        if existing and existing["area"] >= item["area"]:
            continue
        best_by_name[item["name"]] = item

    manifest_rows = []
    for name in sorted(best_by_name):
        item = best_by_name[name]
        filename = sanitize_filename(name) + ".png"
        output_path = OUTPUT_DIR / filename
        output_path.write_bytes(item["png_bytes"])
        manifest_rows.append(
            {
                "name": item["name"],
                "file": filename,
                "source_pdf": item["source_pdf"],
                "page": item["page"],
            }
        )

    with MANIFEST_PATH.open("w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["name", "file", "source_pdf", "page"])
        writer.writeheader()
        writer.writerows(manifest_rows)

    print(f"saved {len(manifest_rows)} files to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
