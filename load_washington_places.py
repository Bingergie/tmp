#!/usr/bin/env python3
"""
Create the washington_places SQLite database and load a places CSV into it.

Usage:
    python load_washington_places.py places.csv
    python load_washington_places.py places.csv --db washington_places.db --reset

Expected CSV columns:
    Place ID, Name, Address, Phone, Rating, Total Reviews Count,
    Website, Categories, Reviews Summary
"""

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS places (
    place_id       TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    address        TEXT,
    phone          TEXT,
    website        TEXT,
    rating         REAL CHECK (rating BETWEEN 0 AND 5),
    reviews_count  INTEGER CHECK (reviews_count >= 0),
    scraped_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS categories (
    category_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS place_categories (
    place_id       TEXT    NOT NULL REFERENCES places(place_id)         ON DELETE CASCADE,
    category_id    INTEGER NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE,
    PRIMARY KEY (place_id, category_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS reviews (
    review_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id       TEXT    NOT NULL REFERENCES places(place_id) ON DELETE CASCADE,
    star_rating    INTEGER NOT NULL CHECK (star_rating BETWEEN 1 AND 5),
    relative_time  TEXT,
    review_text    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_place_id          ON reviews(place_id);
CREATE INDEX IF NOT EXISTS idx_place_categories_category ON place_categories(category_id);
CREATE INDEX IF NOT EXISTS idx_places_name               ON places(name);

CREATE VIEW IF NOT EXISTS place_summary AS
SELECT p.place_id,
       p.name,
       p.address,
       p.rating,
       p.reviews_count,
       p.website,
       GROUP_CONCAT(c.name, ', ') AS categories
FROM places p
LEFT JOIN place_categories pc ON pc.place_id = p.place_id
LEFT JOIN categories c        ON c.category_id = pc.category_id
GROUP BY p.place_id;
"""

# Reviews are joined with "||". Only split where a new "[<n>★" header follows,
# so a stray "||" inside review text can't break a review in two.
REVIEW_SPLIT_RE = re.compile(r"\s*\|\|\s*(?=\[\s*\d\s*★)")
# "[5★ | 2 months ago] review text"
REVIEW_RE = re.compile(r"^\[\s*(\d)\s*★\s*\|\s*([^\]]*?)\s*\]\s*(.*)$", re.DOTALL)
# Some reviews repeat the rating as star emojis at the start of the text.
LEADING_STARS_RE = re.compile(r"^[⭐★☆\s]+")

NULL_TOKENS = {"", "n/a", "na", "none", "null", "-"}


def clean(value):
    """Strip whitespace; turn placeholders like 'N/A' into None."""
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in NULL_TOKENS else value


def to_float(value):
    value = clean(value)
    if value is None:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def to_int(value):
    value = clean(value)
    if value is None:
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


def parse_categories(raw):
    """'bridge, landmark, tourist_attraction' -> ['bridge', 'landmark', ...] (deduped, ordered)."""
    raw = clean(raw)
    if raw is None:
        return []
    seen, result = set(), []
    for part in raw.split(","):
        name = part.strip().lower()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def parse_reviews(raw):
    """Split the 'Reviews Summary' cell into (star_rating, relative_time, text) tuples."""
    raw = clean(raw)
    if raw is None:
        return []
    reviews = []
    for chunk in REVIEW_SPLIT_RE.split(raw):
        match = REVIEW_RE.match(chunk.strip())
        if not match:
            continue  # malformed chunk; skip rather than fail the whole load
        stars, when, text = match.groups()
        stars = int(stars)
        if not 1 <= stars <= 5:
            continue
        text = LEADING_STARS_RE.sub("", text).strip() or None
        reviews.append((stars, when.strip() or None, text))
    return reviews


def load(csv_path, db_path, reset=False):
    if reset and Path(db_path).exists():
        Path(db_path).unlink()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)

    stats = {"places": 0, "reviews": 0, "skipped": 0}

    with open(csv_path, newline="", encoding="utf-8-sig") as f, conn:
        reader = csv.DictReader(f)
        required = {"Place ID", "Name"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

        for row in reader:
            place_id = clean(row.get("Place ID"))
            name = clean(row.get("Name"))
            if not place_id or not name:
                stats["skipped"] += 1
                continue

            # Upsert the place itself.
            conn.execute(
                """
                INSERT INTO places (place_id, name, address, phone, website, rating, reviews_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(place_id) DO UPDATE SET
                    name          = excluded.name,
                    address       = excluded.address,
                    phone         = excluded.phone,
                    website       = excluded.website,
                    rating        = excluded.rating,
                    reviews_count = excluded.reviews_count,
                    scraped_at    = datetime('now')
                """,
                (
                    place_id,
                    name,
                    clean(row.get("Address")),
                    clean(row.get("Phone")),
                    clean(row.get("Website")),
                    to_float(row.get("Rating")),
                    to_int(row.get("Total Reviews Count")),
                ),
            )

            # Replace child rows so re-running the script doesn't duplicate them.
            conn.execute("DELETE FROM place_categories WHERE place_id = ?", (place_id,))
            conn.execute("DELETE FROM reviews WHERE place_id = ?", (place_id,))

            for cat in parse_categories(row.get("Categories")):
                conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat,))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO place_categories (place_id, category_id)
                    SELECT ?, category_id FROM categories WHERE name = ?
                    """,
                    (place_id, cat),
                )

            reviews = parse_reviews(row.get("Reviews Summary"))
            conn.executemany(
                """
                INSERT INTO reviews (place_id, star_rating, relative_time, review_text)
                VALUES (?, ?, ?, ?)
                """,
                [(place_id, s, w, t) for s, w, t in reviews],
            )

            stats["places"] += 1
            stats["reviews"] += len(reviews)

    n_cats = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    conn.close()

    print(f"Database: {db_path}")
    print(f"  places loaded:   {stats['places']}")
    print(f"  reviews loaded:  {stats['reviews']}")
    print(f"  categories:      {n_cats}")
    if stats["skipped"]:
        print(f"  rows skipped (missing Place ID or Name): {stats['skipped']}")


def main():
    parser = argparse.ArgumentParser(description="Load places CSV into a SQLite database.")
    parser.add_argument("csv_path", help="Path to the places CSV file")
    parser.add_argument("--db", default="washington_places.db", help="SQLite file to create (default: washington_places.db)")
    parser.add_argument("--reset", action="store_true", help="Delete the database file first and start fresh")
    args = parser.parse_args()

    if not Path(args.csv_path).is_file():
        sys.exit(f"CSV not found: {args.csv_path}")
    load(args.csv_path, args.db, reset=args.reset)


if __name__ == "__main__":
    main()
